"""
Админка Кати. Одна страница / со статусом (ассеты StayPresent) + управление.
Супервизор l6.py / l6_tg.py пишет в status_registry.
Активный модуль: active_module.txt > env ACTIVE_MODULE > conf.yaml > discord.
UI (title/theme/accent/секции/тексты/футер) читается из conf.yaml на каждый запрос.
Роли — из env-переменных ADMIN_TOKEN_OWNER/ADMIN_TOKEN_ADMIN/ADMIN_TOKEN_JUNIOR.
"""

import urllib.request
import urllib.error
import html as html_escape
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, request

from status_registry import (
    reset as registry_reset,
    mark_started,
    mark_clean_exit,
    mark_crashed,
    mark_permanently_failed,
    append_log,
    update_service_override,
    snapshot,
)
from status_assets import STATUS_CSS, STATUS_HTML_TEMPLATE, STATUS_JS

try:
    from waitress import serve as waitress_serve
except ImportError:
    waitress_serve = None


# ============================================================
# НАСТРОЙКИ
# ============================================================
PORT = int(os.getenv("PORT", 8080))
PROMPT_FILE = Path("system_prompt.txt")
CONF_FILE = Path("conf.yaml")
RUNTIME_MODULE_FILE = Path("active_module.txt")   # пишется админкой

MODULE_FILES = {
    "discord":  ("l6.py",    "kate-bot (discord)",  "Discord-бот Катя"),
    "telegram": ("l6_tg.py", "kate-bot (telegram)", "Telegram-бот Катя"),
}


def _resolve_active_module() -> str:
    """Приоритет: runtime-файл > env ACTIVE_MODULE > conf.yaml > 'discord'."""
    try:
        name = RUNTIME_MODULE_FILE.read_text(encoding="utf-8").strip().lower()
        if name in MODULE_FILES:
            return name
    except FileNotFoundError:
        pass

    env = (os.getenv("ACTIVE_MODULE") or "").strip().lower()
    if env in MODULE_FILES:
        return env

    try:
        with CONF_FILE.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        name = str(cfg.get("active_module") or "").strip().lower()
        if name in MODULE_FILES:
            return name
    except (FileNotFoundError, yaml.YAMLError):
        pass

    return "discord"


ACTIVE_MODULE = _resolve_active_module()
BOT_FILE, BOT_NAME, BOT_DESCRIPTION = MODULE_FILES[ACTIVE_MODULE]

BOT_INDEX = 0
SUPERVISOR_DELAY = 3.0
MAX_RESTARTS = 5
RESTART_RESET_AFTER = 60.0

SELF_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("SELF_URL")
PING_INTERVAL = 600  # 10 минут

app = Flask(__name__)

_state_lock = threading.Lock()
current_proc = None
_stop = threading.Event()
_supervisor_lock = threading.Lock()


# ============================================================
# РОЛИ И ПРАВА (из env)
# ============================================================
ROLE_PERMISSIONS = {
    "owner": frozenset({"view", "prompt", "restart_bot", "restart_all"}),
    "admin": frozenset({"view", "prompt", "restart_bot"}),
    "junior": frozenset({"view"}),
}

ENV_TO_ROLE = {
    "ADMIN_TOKEN_OWNER": "owner",
    "ADMIN_TOKEN_ADMIN": "admin",
    "ADMIN_TOKEN_JUNIOR": "junior",
    "ADMIN_TOKEN": "owner",  # обратная совместимость со старой переменной
}


def _load_admins() -> dict:
    """{token: role}. Читает env на каждый вызов. Пустые пропускает."""
    result = {}
    for env_name, role in ENV_TO_ROLE.items():
        token = (os.getenv(env_name) or "").strip()
        if not token:
            continue
        if token in result:
            continue
        result[token] = role
    return result


def _get_role() -> str | None:
    token = request.cookies.get("admin_token") or request.args.get("token")
    if not token:
        return None
    return _load_admins().get(token)


def _has_perm(perm: str) -> bool:
    role = _get_role()
    if not role:
        return False
    return perm in ROLE_PERMISSIONS.get(role, frozenset())


# ============================================================
# UI-КОНФИГ ИЗ YAML
# ============================================================
UI_DEFAULTS = {
    "title": "Катя — админка",
    "subtitle": "Real-time system status and incident reports",
    "theme": "dark",
    "accent_color": "#f63b3b",
    "sections": {
        "status": True,
        "controls": True,
        "module": True,
        "prompt": True,
        "footer": True,
    },
    "labels": {
        "controls_heading": "⚙️ Управление",
        "restart_bot": "🔄 Перезапустить бота",
        "hard_restart": "☠️ Жёсткий рестарт",
        "module_heading": "🎛️ Активный модуль",
        "module_discord": "💬 Discord",
        "module_telegram": "📨 Telegram",
        "module_hint": "Переключение перезапустит процесс — Render поднимет заново с новым модулем.",
        "prompt_heading": "📝 Системный промпт",
        "save_prompt": "💾 Сохранить промпт",
        "prompt_hint": "Файл: system_prompt.txt. Изменения применяются со следующего сообщения.",
        "login_heading": "🧹 Катя — админка",
        "login_hint": "Введи токен доступа",
        "login_button": "Войти",
    },
    "footer": {
        "copyright": "",
        "links": [],
    },
}


def _load_ui() -> dict:
    """Читает conf.yaml на каждый вызов. Битый/отсутствующий — дефолты."""
    ui = {
        "sections": dict(UI_DEFAULTS["sections"]),
        "labels": dict(UI_DEFAULTS["labels"]),
        "footer": {
            "copyright": UI_DEFAULTS["footer"]["copyright"],
            "links": list(UI_DEFAULTS["footer"]["links"]),
        },
    }
    for k in ("title", "subtitle", "theme", "accent_color"):
        ui[k] = UI_DEFAULTS[k]

    try:
        with CONF_FILE.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except (FileNotFoundError, yaml.YAMLError) as e:
        print(f"[ui] conf.yaml не прочитан: {e} — беру дефолты", flush=True)
        return ui

    src = cfg.get("ui") or {}
    if not isinstance(src, dict):
        return ui

    for key in ("title", "subtitle", "theme", "accent_color"):
        if isinstance(src.get(key), str):
            ui[key] = src[key]

    if isinstance(src.get("sections"), dict):
        for k, v in src["sections"].items():
            if isinstance(v, bool):
                ui["sections"][k] = v

    if isinstance(src.get("labels"), dict):
        for k, v in src["labels"].items():
            if isinstance(v, str):
                ui["labels"][k] = v

    if isinstance(src.get("footer"), dict):
        if isinstance(src["footer"].get("copyright"), str):
            ui["footer"]["copyright"] = src["footer"]["copyright"]
        if isinstance(src["footer"].get("links"), list):
            ui["footer"]["links"] = [
                lk for lk in src["footer"]["links"]
                if isinstance(lk, dict) and lk.get("label") and lk.get("url")
            ]

    return ui


def _hex_shift(hex_color: str, delta: int) -> str:
    """Сдвигает hex на delta (может быть отрицательным). Для --primary-dark."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return hex_color
    r = max(0, min(255, r + delta))
    g = max(0, min(255, g + delta))
    b = max(0, min(255, b + delta))
    return f"#{r:02x}{g:02x}{b:02x}"


# ============================================================
# SELF-PING
# ============================================================
def _self_ping():
    if not SELF_URL:
        print("[ping] RENDER_EXTERNAL_URL/SELF_URL не задан — self-ping выключен", flush=True)
        return

    url = SELF_URL.rstrip("/") + "/health"
    print(f"[ping] старт: {url} каждые {PING_INTERVAL}s", flush=True)

    while not _stop.is_set():
        if _stop.wait(timeout=PING_INTERVAL):
            return
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kate-self-ping"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                print(f"[ping] {url} → {resp.status}", flush=True)
        except urllib.error.HTTPError as e:
            print(f"[ping] {url} → HTTP {e.code}", flush=True)
        except Exception as e:
            print(f"[ping] {url} → ошибка: {e}", flush=True)


# ============================================================
# СУПЕРВИЗОР
# ============================================================
def _supervise():
    global current_proc

    if not _supervisor_lock.acquire(blocking=False):
        print("[supervisor] уже запущен, второй поток не стартую", flush=True)
        return

    try:
        restarts = 0
        started_at = None

        while not _stop.is_set():
            try:
                proc = subprocess.Popen(
                    [sys.executable, BOT_FILE],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except OSError as e:
                print(f"[supervisor] не смог запустить {BOT_FILE}: {e}", flush=True)
                mark_permanently_failed(BOT_INDEX, 1)
                return

            with _state_lock:
                current_proc = proc
            started_at = time.monotonic()
            mark_started(BOT_INDEX)
            print(f"[supervisor] {BOT_FILE} запущен (PID {proc.pid})", flush=True)

            try:
                for line in iter(proc.stdout.readline, ""):
                    line = line.rstrip("\n")
                    print(f"[{BOT_FILE}] {line}", flush=True)
                    append_log(BOT_INDEX, "stdout", line)
            except Exception as e:
                print(f"[supervisor] ошибка чтения stdout: {e}", flush=True)

            proc.wait()
            with _state_lock:
                current_proc = None

            if _stop.is_set():
                return

            code = proc.returncode
            uptime = time.monotonic() - (started_at or time.monotonic())

            if code == 0:
                print(f"[supervisor] {BOT_FILE} завершился штатно", flush=True)
                mark_clean_exit(BOT_INDEX)
                return

            if uptime >= RESTART_RESET_AFTER and restarts > 0:
                print(f"[supervisor] uptime {uptime:.0f}s — сбрасываю счётчик", flush=True)
                restarts = 0

            if restarts >= MAX_RESTARTS:
                print(f"[supervisor] лимит рестартов ({MAX_RESTARTS})", flush=True)
                mark_permanently_failed(BOT_INDEX, code)
                return

            restarts += 1
            print(f"[supervisor] {BOT_FILE} упал (код {code}), "
                  f"рестарт {restarts}/{MAX_RESTARTS}", flush=True)
            mark_crashed(BOT_INDEX, code, will_restart=True)

            if _stop.wait(timeout=SUPERVISOR_DELAY):
                return
    finally:
        _supervisor_lock.release()


def _restart_bot():
    with _state_lock:
        proc = current_proc
    if proc is not None:
        try:
            proc.terminate()
        except OSError:
            pass


# ============================================================
# HTML
# ============================================================
def _read_prompt() -> str:
    try:
        return PROMPT_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _module_button(module_key: str, label: str) -> str:
    active = module_key == ACTIVE_MODULE
    if active:
        style = ("background:var(--primary-color);color:white;border:none;"
                 "cursor:default;opacity:0.9;")
        onclick_attr = " disabled"
        suffix = " ✓"
    else:
        style = ("background:var(--white);color:var(--dark);"
                 "border:1px solid var(--border-color);cursor:pointer;")
        onclick_attr = (
            " onclick=\"if(confirm('Переключить на " + module_key +
            "? Процесс перезапустится.'))moduleSwitch('" + module_key + "')\""
        )
        suffix = ""
    return (
        '<button type="button"' + onclick_attr +
        ' style="padding:10px 20px;border-radius:8px;font-weight:600;' + style + '">' +
        html_escape.escape(label) + suffix + '</button>'
    )


def _build_admin_html() -> str:
    ui = _load_ui()
    l = ui["labels"]
    sections = dict(ui["sections"])

    # Скрываем секции по правам текущей роли
    if not _has_perm("prompt"):
        sections["prompt"] = False
    if not _has_perm("restart_bot") and not _has_perm("restart_all"):
        sections["controls"] = False
    if not _has_perm("restart_all"):
        sections["module"] = False

    prompt_value = _read_prompt()
    prompt_escaped = (
        html_escape.escape(prompt_value)
        if prompt_value
        else "— пусто, бот использует встроенный дефолт —"
    )

    role = _get_role() or "?"
    role_badge = f"""
      <div style="background:var(--light-secondary);padding:10px 16px;border-radius:8px;
                  margin-bottom:20px;font-size:0.9em;color:var(--gray);display:flex;
                  justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
        <span>Роль: <b style="color:var(--primary-color)">{html_escape.escape(role)}</b>
              &nbsp;·&nbsp; Модуль: <b style="color:var(--primary-color)">{html_escape.escape(ACTIVE_MODULE)}</b>
              &nbsp;·&nbsp; Файл: <code>{html_escape.escape(BOT_FILE)}</code>
        </span>
        <button type="button"
                onclick="fetch('/logout',{{method:'POST'}}).then(()=>location.reload())"
                style="padding:6px 14px;border-radius:6px;border:none;background:var(--danger-color);color:white;cursor:pointer;font-weight:600;font-size:0.85em;">
          🚪 Выйти
        </button>
      </div>
    """

    parts = []
    if sections.get("controls"):
        parts.append(f"""
        <h2 style="font-size:1.5em;margin-bottom:20px;font-weight:700;">{html_escape.escape(l['controls_heading'])}</h2>
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:30px;">
          <button type="button" onclick="if(confirm('Перезапустить {BOT_FILE}?'))adminPost('/restart-bot')"
                  style="padding:10px 20px;border-radius:8px;border:none;background:var(--primary-color);color:white;cursor:pointer;font-weight:600;">
            {html_escape.escape(l['restart_bot'])}
          </button>
          <button type="button" onclick="if(confirm('Жёсткий рестарт? Render поднимет заново.'))adminPost('/restart-all')"
                  style="padding:10px 20px;border-radius:8px;border:none;background:var(--danger-color);color:white;cursor:pointer;font-weight:600;">
            {html_escape.escape(l['hard_restart'])}
          </button>
        </div>
        """)

    if sections.get("module"):
        parts.append(f"""
        <h2 style="font-size:1.2em;margin:30px 0 12px;font-weight:700;">
          {html_escape.escape(l['module_heading'])}
        </h2>
        <div style="display:flex;gap:10px;flex-wrap:wrap;">
          {_module_button('discord', l['module_discord'])}
          {_module_button('telegram', l['module_telegram'])}
        </div>
        <p style="color:var(--gray);font-size:0.85em;margin-top:8px;">
          {html_escape.escape(l['module_hint'])}
        </p>
        """)

    if sections.get("prompt"):
        parts.append(f"""
        <h2 style="font-size:1.2em;margin-bottom:12px;font-weight:600;">{html_escape.escape(l['prompt_heading'])}</h2>
        <form method="POST" action="/prompt">
          <textarea name="prompt"
                    style="width:100%;min-height:260px;padding:16px;border:1px solid var(--border-color);border-radius:10px;background:var(--white);color:var(--dark);font-family:'SFMono-Regular',Consolas,monospace;font-size:0.9em;line-height:1.5;resize:vertical;">{prompt_escaped}</textarea>
          <div style="margin-top:12px;">
            <button type="submit"
                    style="padding:10px 20px;border-radius:8px;border:none;background:var(--primary-color);color:white;cursor:pointer;font-weight:600;">
              {html_escape.escape(l['save_prompt'])}
            </button>
          </div>
        </form>
        <p style="color:var(--gray);font-size:0.85em;margin-top:8px;">
          {html_escape.escape(l['prompt_hint'])}
        </p>
        """)

    admin_section = ""
    if parts or role_badge:
        admin_section = (
            '<section class="admin-section" '
            'style="margin-top:50px;padding-top:30px;border-top:2px solid var(--border-color);">'
            + role_badge
            + "".join(parts)
            + """<script>
async function adminPost(url){
  const r = await fetch(url, {method:"POST"});
  const j = await r.json().catch(()=>({}));
  alert(JSON.stringify(j));
}
async function moduleSwitch(name){
  const r = await fetch('/switch-module', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({module: name})
  });
  const j = await r.json().catch(()=>({}));
  if (!r.ok) { alert(JSON.stringify(j)); return; }
  document.body.innerHTML = '<div style="padding:60px;text-align:center;font-family:sans-serif;color:var(--dark)">⏳ Переключение на ' + name + '… страница обновится автоматически</div>';
  const wait = setInterval(async () => {
    try {
      const h = await fetch('/health', {cache: 'no-store'});
      if (h.ok) { clearInterval(wait); location.reload(); }
    } catch (e) {}
  }, 2000);
}
</script>"""
            + "</section>"
        )

    html = STATUS_HTML_TEMPLATE

    accent = ui["accent_color"]
    accent_dark = _hex_shift(accent, -16)

    theme = ui["theme"] if ui["theme"] in ("dark", "light", "auto") else "dark"
    theme_attr = "" if theme == "auto" else f' data-theme="{theme}"'
    color_scheme = "light dark" if theme == "auto" else theme

    override_css = f":root {{ --primary-color: {accent}; --primary-dark: {accent_dark}; }}"

    html = html.replace(
        '<link rel="stylesheet" href="__STAYPRESENT_CSS_URL__" />',
        f"<style>{STATUS_CSS}</style><style>{override_css}</style>",
    )
    html = html.replace(
        '<script src="__STAYPRESENT_JS_URL__"></script>',
        f"<script>{STATUS_JS}</script>",
    )
    html = html.replace(
        '<link rel="icon" href="__STAYPRESENT_FAVICON_URL__" id="favicon">',
        '<link rel="icon" href="data:," id="favicon">',
    )

    html = html.replace("__STAYPRESENT_TITLE__", html_escape.escape(ui["title"]))
    html = html.replace("__STAYPRESENT_DESCRIPTION__", html_escape.escape(ui["subtitle"]))
    html = html.replace("__STAYPRESENT_COLOR_SCHEME__", color_scheme)
    html = html.replace("__STAYPRESENT_THEME_ATTR__", theme_attr)
    html = html.replace("__STAYPRESENT_OG_TAGS_BLOCK__", "")
    html = html.replace("__STAYPRESENT_FAVICON_SCRIPT__", "")

    if sections.get("footer"):
        copyright_text = (ui["footer"].get("copyright") or "").strip()
        copyright_block = f'<p>{html_escape.escape(copyright_text)}</p>' if copyright_text else ""
        links = ui["footer"].get("links") or []
        if links:
            link_html = " | ".join(
                f'<a href="{html_escape.escape(lk["url"], quote=True)}">'
                f'{html_escape.escape(lk["label"])}</a>'
                for lk in links
            )
            links_block = f'<div class="footer-links">{link_html}</div>'
        else:
            links_block = ""
    else:
        copyright_block = ""
        links_block = ""
        html = html.replace('<div class="powered-by">', '<div class="powered-by" hidden>')

    html = html.replace("__STAYPRESENT_COPYRIGHT_BLOCK__", copyright_block)
    html = html.replace("__STAYPRESENT_FOOTER_LINKS_BLOCK__", links_block)

    if not sections.get("status"):
        hide_css = (
            "<style>"
            "body > .container > header,"
            "body > .container > .status-summary,"
            "body > .container > .services,"
            "body > .container > .incidents,"
            "body > .container > .metrics"
            "{display:none !important;}"
            "</style>"
        )
        html = html.replace("</head>", hide_css + "</head>")

    if admin_section:
        html = html.replace("      <footer>", admin_section + "\n      <footer>", 1)

    return html


def _login_page(err: str = "") -> str:
    ui = _load_ui()
    l = ui["labels"]
    accent = ui["accent_color"]
    theme = ui["theme"] if ui["theme"] in ("dark", "light", "auto") else "dark"

    if theme == "light":
        bg, card, line, text, muted, input_bg = "#f8fafc", "#ffffff", "#e2e8f0", "#0f172a", "#64748b", "#f1f5f9"
    else:
        bg, card, line, text, muted, input_bg = "#0b1220", "#16202f", "#2d3b4e", "#e6edf3", "#94a3b8", "#0b1220"

    e = f'<p style="color:#f87171;margin:0 0 1rem">{html_escape.escape(err)}</p>' if err else ""

    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_escape.escape(ui['title'])}</title>
<style>
  body{{background:{bg};color:{text};font-family:-apple-system,sans-serif;
        display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;}}
  .box{{background:{card};padding:2rem;border-radius:12px;border:1px solid {line};width:360px;}}
  h1{{color:{accent};margin:0 0 0.5rem;font-size:1.4rem;}}
  p{{color:{muted};margin:0 0 1rem;font-size:.9rem;}}
  input{{width:100%;padding:10px 14px;margin:0 0 1rem;border:1px solid {line};border-radius:8px;
         background:{input_bg};color:{text};box-sizing:border-box;font-size:1rem;}}
  button{{width:100%;padding:10px;background:{accent};color:white;border:none;border-radius:8px;
          cursor:pointer;font-weight:600;font-size:1rem;}}
  button:hover{{opacity:.9;}}
</style></head><body>
<div class="box">
  <h1>{html_escape.escape(l['login_heading'])}</h1>
  <p>{html_escape.escape(l['login_hint'])}</p>
  {e}
  <form method="POST" action="/login">
    <input type="password" name="token" placeholder="ADMIN_TOKEN" autocomplete="off">
    <button type="submit">{html_escape.escape(l['login_button'])}</button>
  </form>
</div>
</body></html>"""


# ============================================================
# РОУТЫ
# ============================================================
@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    if not _load_admins():
        return Response(
            "<h1 style='font-family:sans-serif;text-align:center;margin-top:4rem'>"
            "Ни одного ADMIN_TOKEN_* не задано в env</h1>",
            status=503,
        )
    if not _has_perm("view"):
        return Response(_login_page(), mimetype="text/html")
    return Response(_build_admin_html(), mimetype="text/html")


@app.route("/login", methods=["POST"])
def login():
    token = (request.form.get("token") or "").strip()
    admins = _load_admins()
    if token and token in admins:
        resp = Response(status=302)
        resp.headers["Location"] = "/"
        resp.set_cookie("admin_token", token, httponly=True, samesite="Strict")
        return resp
    return Response(_login_page("Неверный токен"), status=403, mimetype="text/html")


@app.route("/logout", methods=["POST"])
def logout():
    resp = Response(status=302)
    resp.headers["Location"] = "/"
    resp.delete_cookie("admin_token")
    return resp


@app.route("/api/status.json")
def api_status_json():
    if not _has_perm("view"):
        return jsonify({"error": "forbidden"}), 403

    timezone = "auto"
    time_format = "24h"
    try:
        with CONF_FILE.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        ui_cfg = cfg.get("ui") or {}
        if isinstance(ui_cfg, dict):
            if isinstance(ui_cfg.get("timezone"), str):
                timezone = ui_cfg["timezone"]
            if isinstance(ui_cfg.get("time_format"), str):
                time_format = ui_cfg["time_format"]
    except (FileNotFoundError, yaml.YAMLError):
        pass

    data = snapshot(admin=True, timezone=timezone, time_format=time_format)
    data["admin_available"] = False
    data["poll_seconds"] = 10
    return jsonify(data)


@app.route("/prompt", methods=["POST"])
def save_prompt():
    if not _has_perm("prompt"):
        return Response("forbidden", status=403)
    text = (request.form.get("prompt") or "").strip()
    if text and not text.startswith("—"):
        PROMPT_FILE.write_text(text, encoding="utf-8")
    return Response(status=302, headers={"Location": "/"})


@app.route("/restart-bot", methods=["POST"])
def restart_bot_route():
    if not _has_perm("restart_bot"):
        return jsonify({"error": "forbidden"}), 403
    _restart_bot()
    return jsonify({"status": "restarting " + BOT_FILE})


@app.route("/restart-all", methods=["POST"])
def restart_all_route():
    if not _has_perm("restart_all"):
        return jsonify({"error": "forbidden"}), 403
    threading.Timer(1.0, lambda: os._exit(0)).start()
    return jsonify({"status": "dying"})


@app.route("/switch-module", methods=["POST"])
def switch_module_route():
    if not _has_perm("restart_all"):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or request.form
    module = str((data or {}).get("module") or "").strip().lower()

    if module not in MODULE_FILES:
        return jsonify({
            "error": f"unknown module: {module!r}",
            "allowed": list(MODULE_FILES.keys()),
        }), 400

    if module == ACTIVE_MODULE:
        return jsonify({"status": "already_active", "module": module})

    try:
        RUNTIME_MODULE_FILE.write_text(module + "\n", encoding="utf-8")
    except OSError as e:
        return jsonify({"error": f"can't write {RUNTIME_MODULE_FILE}: {e}"}), 500

    print(f"[main] switch-module: {ACTIVE_MODULE} → {module}, рестарт", flush=True)
    threading.Timer(1.0, lambda: os._exit(0)).start()
    return jsonify({"status": "switching", "from": ACTIVE_MODULE, "to": module})


# ============================================================
# ЗАПУСК
# ============================================================
def _shutdown(signum, frame):
    print(f"[main] сигнал {signum}, останавливаюсь", flush=True)
    _stop.set()
    with _state_lock:
        proc = current_proc
    if proc is not None:
        try:
            proc.terminate()
        except OSError:
            pass
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(f"[main] активный модуль: {ACTIVE_MODULE} → {BOT_FILE}", flush=True)

    registry_reset(
        [{"file": BOT_FILE, "module": None, "args": [], "env": {}, "status": None}],
        display_names=[BOT_NAME],
        track_web_server=False,
        bot_status=[True],
    )
    update_service_override(BOT_FILE, BOT_NAME, BOT_DESCRIPTION)

    threading.Thread(target=_supervise, daemon=True).start()
    threading.Thread(target=_self_ping, daemon=True).start()

    if waitress_serve is not None:
        print(f"[main] waitress на порту {PORT}", flush=True)
        waitress_serve(app, host="0.0.0.0", port=PORT, threads=4)
    else:
        print(f"[main] flask dev-server на порту {PORT}", flush=True)
        app.run(host="0.0.0.0", port=PORT, threaded=True)