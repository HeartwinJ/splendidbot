import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from config import (
    LOGIN_TIMEOUT_MS,
    ONSINCH_BASE_URL,
    POSITIONS_API_BODY,
    POSITIONS_API_HEADERS,
    SESSIONS_DIR,
    API_RETRY_COUNT,
)

logger = logging.getLogger(__name__)

COOKIE_NAME = "Sinch_app_cookie_splendid"


def _session_path(chat_id: int) -> str:
    return os.path.join(SESSIONS_DIR, f"{chat_id}.json")


def _read_cookie_from_state(chat_id: int) -> Optional[str]:
    path = _session_path(chat_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            state = json.load(f)
        for cookie in state.get("cookies", []):
            if cookie.get("name") == COOKIE_NAME:
                return cookie["value"]
    except Exception as exc:
        logger.warning("Failed to read session state for chat_id %s: %s", chat_id, exc)
    return None


async def _playwright_login(email: str, password: str, chat_id: int) -> str:
    from playwright_stealth import stealth_async

    logger.info("Launching Playwright for login (chat_id=%s)", chat_id)
    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        context: BrowserContext = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) "
                "Gecko/20100101 Firefox/148.0"
            ),
        )

        page: Page = await context.new_page()
        await stealth_async(page)

        try:
            await page.goto(f"{ONSINCH_BASE_URL}/", timeout=LOGIN_TIMEOUT_MS)

            email_selector = 'input[name="email"]'
            try:
                await page.wait_for_selector(email_selector, state="visible", timeout=10000)
            except Exception:
                for sel in ['input[type="email"]', 'input[type="text"]']:
                    try:
                        await page.wait_for_selector(sel, state="visible", timeout=5000)
                        email_selector = sel
                        break
                    except Exception:
                        continue
                else:
                    raise RuntimeError("Could not find email input field on login page")

            await page.fill(email_selector, email)
            await page.fill('input[type="password"]', password)

            async with page.expect_navigation(timeout=LOGIN_TIMEOUT_MS):
                await page.click('input[type="submit"]')

            await asyncio.sleep(1)

            cookies = await context.cookies()
            cookie_value = next(
                (c["value"] for c in cookies if c["name"] == COOKIE_NAME), None
            )
            if not cookie_value:
                logger.warning(
                    "Login failed for chat_id=%s — session cookie absent after submit (url=%s)",
                    chat_id,
                    page.url,
                )
                raise RuntimeError("Login failed: invalid credentials or reCAPTCHA challenge")

            os.makedirs(SESSIONS_DIR, exist_ok=True)
            await context.storage_state(path=_session_path(chat_id))
            logger.info("Session saved for chat_id=%s", chat_id)

            return cookie_value
        finally:
            await context.close()
            await browser.close()


async def _call_positions_api(cookie_value: str) -> dict:
    headers = {**POSITIONS_API_HEADERS, "Cookie": f"{COOKIE_NAME}={cookie_value}"}

    last_exc: Optional[Exception] = None
    for attempt in range(1, API_RETRY_COUNT + 1):
        try:
            async with httpx.AsyncClient(follow_redirects=False, timeout=20) as client:
                response = await client.post(
                    f"{ONSINCH_BASE_URL}/api",
                    json=POSITIONS_API_BODY,
                    headers=headers,
                )
                if response.status_code in (301, 302, 303, 307, 308):
                    raise httpx.HTTPStatusError(
                        "Session expired (redirect)",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                try:
                    return response.json()
                except Exception as exc:
                    raise RuntimeError(
                        f"API returned non-JSON response (status {response.status_code})"
                    ) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403) or "Session expired" in str(exc):
                raise
            last_exc = exc
        except Exception as exc:
            last_exc = exc

        if attempt < API_RETRY_COUNT:
            wait = 2 ** attempt
            logger.warning(
                "API call attempt %d/%d failed, retrying in %ds: %s",
                attempt,
                API_RETRY_COUNT,
                wait,
                last_exc,
            )
            await asyncio.sleep(wait)

    raise RuntimeError(f"API call failed after {API_RETRY_COUNT} attempts") from last_exc


def _is_session_expired_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (401, 403) or "Session expired" in str(exc)
    return False


def _filter_and_parse(data: dict) -> list[dict]:
    try:
        entities = data["entities"]
        position_ids = data["result"]["positionIds"]
    except (KeyError, TypeError) as exc:
        logger.error("Unexpected API response structure: %s", exc)
        return []

    positions = entities.get("Position", {})
    shifts = entities.get("Shift", {})
    professions = entities.get("Profession", {})
    locations = entities.get("Location", {})
    attendances = entities.get("PositionAttendance", {})

    booked_position_ids: set[int] = set()
    if isinstance(attendances, dict):
        for att in attendances.values():
            booked_position_ids.add(att["position"])

    now_utc = datetime.now(timezone.utc)
    result = []

    for pos_id in position_ids:
        pos = positions.get(str(pos_id))
        if pos is None:
            continue

        if pos.get("role") == 1:
            continue
        if pos.get("cancelled"):
            continue
        if pos.get("hidden"):
            continue
        if pos.get("freeCapacity", 0) <= 0:
            continue
        if pos["id"] in booked_position_ids:
            continue

        start_time_str = pos["startTime"]
        try:
            start_dt = datetime.fromisoformat(start_time_str)
            if start_dt < now_utc:
                continue
        except ValueError:
            pass

        shift = shifts.get(str(pos["shift"]), {})
        profession = professions.get(str(pos["profession"]), {})
        location = locations.get(str(pos["location"]), {})

        organizer_name = ""
        company_id = shift.get("company") or shift.get("organizer")
        if company_id:
            companies = entities.get("Company", {})
            company = companies.get(str(company_id), {})
            organizer_name = company.get("name", "")

        result.append({
            "id": pos["id"],
            "shift_id": pos["shift"],
            "shift_name": shift.get("name", "Unknown Shift"),
            "organizer": organizer_name,
            "profession": profession.get("name", "Unknown Profession"),
            "location": location.get("name", "Unknown Location"),
            "title": pos.get("title", "").strip(),
            "start_time": pos["startTime"],
            "end_time": pos["endTime"],
            "free_capacity": pos.get("freeCapacity", 0),
            "total_capacity": pos.get("totalCapacity", 0),
            "role": pos.get("role", 0),
            "featured": pos.get("featured", False),
            "in_conflict": pos.get("inConflict", False),
            "conflicting_positions": pos.get("conflicting", {}).get("position", []),
            "requirements_failed": pos.get("requirementsFailed", False),
            "applicants": bool(pos.get("applicants", False)),
        })

    return result


async def scrape_positions(chat_id: int, email: str, password: str) -> list[dict]:
    cookie = _read_cookie_from_state(chat_id)

    if cookie:
        try:
            data = await _call_positions_api(cookie)
            positions = _filter_and_parse(data)
            logger.info("Scraped %d positions for chat_id=%s (session reused)", len(positions), chat_id)
            return positions
        except Exception as exc:
            if _is_session_expired_error(exc):
                logger.info("Session expired for chat_id=%s, re-logging in", chat_id)
            else:
                raise

    cookie = await _playwright_login(email, password, chat_id)
    data = await _call_positions_api(cookie)
    positions = _filter_and_parse(data)
    logger.info("Scraped %d positions for chat_id=%s (fresh login)", len(positions), chat_id)
    return positions


async def login_and_validate(email: str, password: str, chat_id: int) -> list[dict]:
    path = _session_path(chat_id)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError as exc:
            logger.warning("Could not remove old session file for chat_id=%s: %s", chat_id, exc)

    cookie = await _playwright_login(email, password, chat_id)
    data = await _call_positions_api(cookie)
    return _filter_and_parse(data)
