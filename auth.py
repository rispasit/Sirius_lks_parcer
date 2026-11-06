"""
Авторизация в ЛК Sirius University.

Публичный интерфейс:
    get_session(force_refresh: bool = False) -> requests.Session
    fetch_captcha_result(task_id: int) -> str | None   # добор оплаченной задачи
Все ошибки — подклассы AuthError.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

# Загружаем .env на импорте — компромисс: ленивая инициализация
# потребовала бы переписать все глобалы ниже в dataclass конфига.
load_dotenv()

logger = logging.getLogger(__name__)

# --- config ---
SITE_BASE   = os.getenv("SITE_BASE", "").rstrip("/")
PROBE_URL   = os.getenv("PROBE_URL")
LOGIN_URL   = os.getenv("LOGIN_URL")
LOGIN_POST  = os.getenv("LOGIN_POST_URL")
USER        = os.getenv("SITE_USER")
PASS        = os.getenv("SITE_PASS")
RU_KEY      = os.getenv("RU_API_KEY")
COOKIE_PATH = os.getenv("COOKIE_PATH", "cookies.json")

RU_CREATE = "https://api.rucaptcha.com/createTask"
RU_RESULT = "https://api.rucaptcha.com/getTaskResult"

# --- поведение ---
CAPTCHA_TIMEOUT_S = 200     # максимум ожидания решения
POLL_INTERVAL_S   = 2       # интервал опроса getTaskResult
RETRY_ATTEMPTS    = 3       # попыток на ERROR_NO_SLOT_AVAILABLE
RETRY_PAUSE_S     = 5       # пауза между попытками


# =============================================================== exceptions

class AuthError(RuntimeError):
    """Базовая ошибка авторизации."""


class AuthConfigError(AuthError):
    """В .env не хватает значений."""


class CookieError(AuthError):
    """Cookie не загрузились / битые."""


class LoginFormError(AuthError):
    """Не удалось разобрать форму логина (нет _token / sitekey)."""


class LoginRejectedError(AuthError):
    """Сервер не пустил после POST (неверный пароль, капча и т.п.)."""


# --- captcha ---

class CaptchaError(AuthError):
    """Базовая ошибка капчи."""


class CaptchaConfigError(CaptchaError):
    """Неверный API-ключ / параметры задачи / неподдерживаемый тип."""


class CaptchaBalanceError(CaptchaError):
    """Кончились деньги на RuCaptcha."""


class CaptchaUnsolvableError(CaptchaError):
    """RuCaptcha не смогла решить капчу."""


class CaptchaWrongSitekeyError(CaptchaError):
    """Неверный sitekey / не тот тип капчи."""


class CaptchaTimeoutError(CaptchaError):
    """Не дождались решения за отведённое время."""


class CaptchaServiceError(CaptchaError):
    """Ошибка RuCaptcha (не сеть): 5xx/429/не JSON/пустой ответ."""


class CaptchaNetworkError(CaptchaServiceError):
    """Сеть RuCaptcha: DNS, таймауты, обрыв соединения.

    Отдельный класс, чтобы при poll можно было ретраить:
    задача уже оплачена, сеть моргнула — надо добрать результат.
    """


# карта errorCode RuCaptcha -> (класс, человеко-читаемое сообщение)
_RU_ERRORS = {
    "ERROR_WRONG_USER_KEY":          (CaptchaConfigError,      "неверный API-ключ RuCaptcha"),
    "ERROR_KEY_DOES_NOT_EXIST":      (CaptchaConfigError,      "API-ключ RuCaptcha не существует"),
    "ERROR_ZERO_BALANCE":            (CaptchaBalanceError,     "нулевой баланс RuCaptcha"),
    "ERROR_WRONG_GOOGLEKEY":         (CaptchaWrongSitekeyError,"неверный sitekey капчи"),
    "ERROR_WRONG_CAPTCHA_ID":        (CaptchaWrongSitekeyError,"неверный id капчи"),
    "ERROR_CAPTCHA_UNSOLVABLE":      (CaptchaUnsolvableError,  "капча не решается"),
    "ERROR_BAD_PARAMETERS":          (CaptchaConfigError,      "кривые параметры задачи"),
    "ERROR_TASK_NOT_SUPPORTED":      (CaptchaConfigError,      "тип задачи не поддерживается RuCaptcha"),
    "ERROR_BAD_PROXY":               (CaptchaServiceError,     "плохой прокси"),
    "ERROR_PROXY_CONNECTION_FAILED": (CaptchaServiceError,     "не удалось подключиться к прокси"),
    "ERROR_NO_SLOT_AVAILABLE":       (CaptchaServiceError,     "нет свободных слотов, повтори позже"),
}

# эти ошибки имеет смысл ретраить
_TRANSIENT_ERRORS = {"ERROR_NO_SLOT_AVAILABLE"}


# =============================================================== helpers

@dataclass
class _ProbeResult:
    """Результат проверки PROBE_URL.

    ok: True  — 200, или редирект НЕ на /login
        False — редирект на /login, 401/403, прочие не-200
        None  — не удалось определить (сеть, 5xx) — НЕ повод стирать cookie
    """
    ok: bool | None
    status: int | None = None
    location: str | None = None


class _LoginFormParser(HTMLParser):
    """Достаёт _token и data-sitekey из HTML логин-страницы.

    Устойчивее регексов: не зависит от порядка атрибутов,
    кавычек, регистра тегов.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.csrf: str | None = None
        self.sitekeys: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() == "input" and d.get("name") == "_token" and self.csrf is None:
            self.csrf = d.get("value") or None
        if "data-sitekey" in d and d["data-sitekey"]:
            self.sitekeys.append(d["data-sitekey"])


def _post_json(url: str, payload: dict, timeout: int = 30) -> dict:
    try:
        r = requests.post(url, json=payload, timeout=timeout)
    except requests.RequestException as e:
        raise CaptchaNetworkError(f"сеть RuCaptcha: {e}") from e

    if r.status_code == 429:
        raise CaptchaNetworkError("RuCaptcha: rate limit (HTTP 429)")
    if r.status_code >= 500:
        raise CaptchaNetworkError(f"RuCaptcha HTTP {r.status_code}")

    try:
        return r.json()
    except ValueError as e:
        raise CaptchaNetworkError(
            f"RuCaptcha вернул не JSON (HTTP {r.status_code})"
        ) from e


def _raise_ru_error(resp: dict) -> None:
    code = resp.get("errorCode") or "UNKNOWN"
    desc = (resp.get("errorDescription") or "").strip()
    cls, msg = _RU_ERRORS.get(code, (CaptchaServiceError, f"RuCaptcha: {code}"))
    if desc and desc.lower() not in msg.lower():
        msg = f"{msg} [{desc}]"
    raise cls(msg)


def _validate_config() -> None:
    required = {
        "SITE_BASE":      SITE_BASE,
        "PROBE_URL":      PROBE_URL,
        "LOGIN_URL":      LOGIN_URL,
        "LOGIN_POST_URL": LOGIN_POST,
        "SITE_USER":      USER,
        "SITE_PASS":      PASS,
        "RU_API_KEY":     RU_KEY,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise AuthConfigError(f"не заполнено в .env: {', '.join(missing)}")


def _parse_expires(value) -> int | None:
    """Нормализует expires из cookies.json: int | None."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# =============================================================== cookie io

def _load_cookies(session: requests.Session) -> None:
    try:
        with open(COOKIE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        raise CookieError(f"не читается {COOKIE_PATH}: {e}") from e

    if not isinstance(data, list):
        raise CookieError(f"битый формат {COOKIE_PATH}: ожидался список")

    try:
        for c in data:
            name  = c["name"]
            value = c["value"]
            if not isinstance(name, str) or not isinstance(value, str):
                raise CookieError(
                    f"битый формат {COOKIE_PATH}: name/value должны быть строками"
                )
            session.cookies.set(
                name, value,
                domain=c.get("domain") or None,
                path=c.get("path") or "/",
                secure=bool(c.get("secure", False)),
                expires=_parse_expires(c.get("expires")),
            )
    except KeyError as e:
        raise CookieError(f"битый формат {COOKIE_PATH}: нет поля {e}") from e
    except TypeError as e:
        raise CookieError(f"битый формат {COOKIE_PATH}: {e}") from e


def _save_cookies(session: requests.Session) -> None:
    data = [
        {"name": c.name, "value": c.value,
         "domain": c.domain, "path": c.path,
         "secure": c.secure, "expires": c.expires}
        for c in session.cookies
    ]

    try:
        with open(COOKIE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        raise CookieError(f"не пишется {COOKIE_PATH}: {e}") from e

    # chmod отдельно — на Windows/без прав не должен ломать сохранение
    try:
        os.chmod(COOKIE_PATH, 0o600)
    except OSError:
        pass


# =============================================================== validity

def _is_login_redirect(location: str) -> bool:
    """Редирект указывает на страницу логина?"""
    if not location:
        return False
    path = urlparse(location).path.rstrip("/")
    return path == "/login"


def _is_valid(session: requests.Session) -> _ProbeResult:
    """Проверка PROBE_URL без follow-редиректов.

    Возвращает _ProbeResult:
        ok=True  — 200, или редирект НЕ на /login
        ok=False — редирект на /login, 401/403, прочие не-200
        ok=None  — сеть/5xx, статус неизвестен
    """
    try:
        r = session.get(PROBE_URL, timeout=15, allow_redirects=False)
    except requests.RequestException:
        return _ProbeResult(ok=None)

    if r.status_code >= 500:
        return _ProbeResult(ok=None, status=r.status_code)

    if r.status_code in (301, 302, 303, 307, 308):
        location = r.headers.get("Location", "")
        return _ProbeResult(
            ok=not _is_login_redirect(location),
            status=r.status_code,
            location=location,
        )

    if r.status_code in (401, 403):
        return _ProbeResult(ok=False, status=r.status_code)

    return _ProbeResult(ok=r.status_code == 200, status=r.status_code)


# =============================================================== captcha

def _create_captcha_task(url: str, sitekey: str) -> int:
    payload = {
        "clientKey": RU_KEY,
        "task": {
            "type": "YandexSmartCaptchaTaskProxyless",
            "websiteURL": url,
            "websiteKey": sitekey,
        },
    }

    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = _post_json(RU_CREATE, payload)
        except CaptchaNetworkError:
            # ВАЖНО: createTask НЕ ретраим на сетевых ошибках —
            # не знаем, создалась ли задача, повтор = риск двойного списания.
            raise

        if not resp.get("errorId"):
            task_id = resp.get("taskId")
            if not task_id:
                raise CaptchaServiceError(
                    f"RuCaptcha не вернул taskId: {resp}"
                )
            logger.info("RuCaptcha: задача создана, taskId=%s", task_id)
            return task_id

        code = resp.get("errorCode") or ""
        if code in _TRANSIENT_ERRORS and attempt < RETRY_ATTEMPTS - 1:
            logger.info(
                "RuCaptcha: %s, пауза %dс (попытка %d/%d)",
                code, RETRY_PAUSE_S, attempt + 1, RETRY_ATTEMPTS,
            )
            time.sleep(RETRY_PAUSE_S)
            continue

        _raise_ru_error(resp)            # всегда бросает

    raise CaptchaServiceError("не удалось поставить задачу в RuCaptcha")


def _poll_captcha_result(task_id: int) -> str:
    deadline = time.monotonic() + CAPTCHA_TIMEOUT_S
    polls = 0
    consecutive_net_errors = 0

    while time.monotonic() < deadline:
        polls += 1
        try:
            resp = _post_json(RU_RESULT, {"clientKey": RU_KEY, "taskId": task_id})
            consecutive_net_errors = 0
        except CaptchaNetworkError as e:
            # Задача уже оплачена — продолжаем пытаться, пока есть время.
            consecutive_net_errors += 1
            backoff = min(POLL_INTERVAL_S * consecutive_net_errors, 10)
            logger.warning(
                "RuCaptcha: сеть на опросе %d (%s), пауза %dс, taskId=%s",
                polls, e, backoff, task_id,
            )
            time.sleep(backoff)
            continue

        if resp.get("errorId"):
            code = resp.get("errorCode") or ""
            if code not in _TRANSIENT_ERRORS:
                _raise_ru_error(resp)
            logger.debug("RuCaptcha: %s на опросе %d", code, polls)
        elif resp.get("status") == "ready":
            token = (resp.get("solution") or {}).get("token")
            if not token:
                raise CaptchaServiceError(
                    f"RuCaptcha вернул ready без token: {resp}"
                )
            logger.info("RuCaptcha: решена за %d опросов", polls)
            return token
        else:
            logger.debug("RuCaptcha: status=%s", resp.get("status"))

        time.sleep(POLL_INTERVAL_S)

    logger.warning("RuCaptcha: не дождались результата, taskId=%s", task_id)
    raise CaptchaTimeoutError(
        f"капча не решена за {CAPTCHA_TIMEOUT_S}с "
        f"(taskId={task_id}, опросов={polls})"
    )


def _solve_smartcaptcha(url: str, sitekey: str) -> str:
    task_id = _create_captcha_task(url, sitekey)
    try:
        return _poll_captcha_result(task_id)
    except CaptchaError as e:
        msg = str(e)
        if "taskId" not in msg:
            raise type(e)(
                f"{msg} [taskId={task_id} — можно добрать через "
                f"auth.fetch_captcha_result({task_id})]"
            ) from e
        raise


def fetch_captcha_result(task_id: int) -> str | None:
    """Добрать результат уже созданной задачи. Денег не тратит.

    Возвращает токен, если задача решена, иначе None.
    Полезно, если процесс упал на сети после createTask.
    Работает ~10 минут после создания задачи (потом RuCaptcha её забывает).
    """
    resp = _post_json(RU_RESULT, {"clientKey": RU_KEY, "taskId": task_id})
    if resp.get("errorId"):
        _raise_ru_error(resp)
    if resp.get("status") == "ready":
        return (resp.get("solution") or {}).get("token")
    return None


# =============================================================== login

def _parse_login_form(html: str) -> tuple[str, str]:
    """Возвращает (_token, sitekey) из HTML формы логина."""
    parser = _LoginFormParser()
    parser.feed(html)
    parser.close()

    if not parser.csrf:
        raise LoginFormError("CSRF (_token) не найден в HTML логина")
    if not parser.sitekeys:
        raise LoginFormError("data-sitekey капчи не найден в HTML логина")

    return parser.csrf, parser.sitekeys[0]


def _login(session: requests.Session) -> None:
    try:
        page = session.get(LOGIN_URL, timeout=20).text
    except requests.RequestException as e:
        raise AuthError(f"сеть при загрузке формы логина: {e}") from e

    csrf, sitekey = _parse_login_form(page)
    token = _solve_smartcaptcha(LOGIN_URL, sitekey)

    try:
        r = session.post(LOGIN_POST, data={
            "_token": csrf,
            "login": USER,
            "password": PASS,
            "smart-token": token,
            "remember": "on",
        }, headers={
            "Referer": LOGIN_URL,
            "Origin":  SITE_BASE,
        }, timeout=30)
    except requests.RequestException as e:
        raise AuthError(
            f"сеть при отправке логина: {e}. "
            f"Капча-токен потрачен; повторный запуск создаст новую задачу."
        ) from e

    probe = _is_valid(session)
    if probe.ok is False:
        detail = f"HTTP {probe.status}"
        if probe.location:
            detail += f", Location={probe.location}"
        raise LoginRejectedError(
            f"сервер не авторизовал (probe: {detail}, POST: HTTP {r.status_code})"
        )
    if probe.ok is None:
        logger.warning(
            "не удалось проверить сессию после логина "
            "(probe HTTP %s) — cookie будут сохранены как есть",
            probe.status,
        )


# =============================================================== public

def get_session(force_refresh: bool = False) -> requests.Session:
    """Сессия с валидной cookie. Любая проблема — подкласс AuthError.

    force_refresh=True — логиниться даже если PROBE_URL недоступен
    (по умолчанию при неизвестном статусе бросаем AuthError,
    чтобы зря не жечь капчу).
    """
    _validate_config()

    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Referer": SITE_BASE + "/",
    })

    _load_cookies(s)

    probe = _is_valid(s)
    if probe.ok is True:
        return s

    if probe.ok is None and not force_refresh:
        raise AuthError(
            f"не удалось проверить валидность cookie "
            f"(probe HTTP {probe.status}: сеть или 5xx). "
            f"Передай force_refresh=True, чтобы логиниться вслепую."
        )

    # ok=False, либо ok=None с force_refresh
    s.cookies.clear()
    _login(s)

    try:
        _save_cookies(s)
    except CookieError as e:
        logger.warning("cookie не сохранены на диск: %s", e)

    return s