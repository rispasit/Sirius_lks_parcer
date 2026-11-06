import datetime as dt
import json
import logging
import sys

from auth import (
    get_session,
    AuthError, AuthConfigError,
    CookieError, LoginFormError, LoginRejectedError,
    CaptchaError, CaptchaBalanceError, CaptchaConfigError,
    CaptchaWrongSitekeyError, CaptchaUnsolvableError,
    CaptchaTimeoutError, CaptchaServiceError, CaptchaNetworkError,
)
from parser import get_week, SCHEMA_VERSION, MSK


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def get_session_with_fallback():
    try:
        return get_session()
    except AuthError as e:
        if "probe" in str(e).lower():
            logging.warning(
                "probe недоступен (%s), пробую force_refresh=True", e
            )
            return get_session(force_refresh=True)
        raise


def main() -> int:
    setup_logging()

    try:
        session = get_session_with_fallback()
    except AuthConfigError as e:
        print(f"[конфиг] {e}", file=sys.stderr); return 2
    except CookieError as e:
        print(f"[cookie] {e}", file=sys.stderr); return 3
    except LoginFormError as e:
        print(f"[форма] {e}", file=sys.stderr); return 4
    except CaptchaBalanceError as e:
        print(f"[rucaptcha] {e}", file=sys.stderr); return 5
    except CaptchaConfigError as e:
        print(f"[rucaptcha] {e}", file=sys.stderr); return 5
    except CaptchaWrongSitekeyError as e:
        print(f"[rucaptcha] {e}", file=sys.stderr); return 5
    except CaptchaUnsolvableError as e:
        print(f"[rucaptcha] {e}", file=sys.stderr); return 5
    except CaptchaTimeoutError as e:
        print(f"[rucaptcha] {e}", file=sys.stderr); return 5
    except CaptchaNetworkError as e:
        print(f"[rucaptcha] {e}", file=sys.stderr); return 5
    except CaptchaServiceError as e:
        print(f"[rucaptcha] {e}", file=sys.stderr); return 5
    except LoginRejectedError as e:
        print(f"[логин] {e}", file=sys.stderr); return 6
    except AuthError as e:
        print(f"[auth] {e}", file=sys.stderr); return 1

    logging.info("OK: сессия валидна")

    try:
        current = get_week(session, "current")
        nxt = get_week(session, "next")
    except Exception as e:
        print(f"[парсер] {e}", file=sys.stderr)
        return 7

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at":   int(dt.datetime.now(tz=MSK).timestamp()),
        "tz":             str(MSK),
        "current":        current.to_dict(),
        "next":           nxt.to_dict(),
    }

    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())