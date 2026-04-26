import argparse
import os
from datetime import date, timedelta
from typing import Iterable, List, Set
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from api.client import Account
from api.models.timetable import Lesson


GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Переносит расписание HSE в Google Calendar"
    )
    parser.add_argument(
        "--calendar-id",
        default="primary",
        help="ID календаря Google (по умолчанию: primary)",
    )
    parser.add_argument(
        "--start-date",
        help="Дата начала в формате YYYY-MM-DD (по умолчанию: сегодня)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Количество дней для переноса, начиная со start-date (по умолчанию: 7)",
    )
    parser.add_argument(
        "--timezone",
        default="Europe/Moscow",
        help="Таймзона для событий Google Calendar (по умолчанию: Europe/Moscow)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только показать, какие события будут добавлены, без записи в Google Calendar",
    )
    return parser.parse_args()


def parse_start_date(raw: str | None) -> date:
    if not raw:
        return date.today()
    return date.fromisoformat(raw)


def collect_lessons(account: Account, start_date: date, days: int) -> List[Lesson]:
    unique_lessons: dict[str, Lesson] = {}
    for offset in range(days):
        target_date = start_date + timedelta(days=offset)
        for lesson in account.get_timetable(start_date=target_date):
            unique_lessons[lesson.id] = lesson
    return sorted(unique_lessons.values(), key=lambda l: l.date_start)


def google_events_url(calendar_id: str) -> str:
    encoded_calendar_id = quote(calendar_id, safe="")
    return f"{GOOGLE_CALENDAR_API}/calendars/{encoded_calendar_id}/events"


def google_headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


class GoogleCalendarAuth:
    def __init__(
        self,
        access_token: str | None,
        refresh_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret

    @property
    def can_refresh(self) -> bool:
        return bool(self._refresh_token and self._client_id and self._client_secret)

    def ensure_access_token(self) -> None:
        if self._access_token:
            return
        if not self.can_refresh:
            raise RuntimeError(
                "Нет Google access token, а для обновления нужны GOOGLE_REFRESH_TOKEN, GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET"
            )
        self.refresh_access_token()

    def refresh_access_token(self) -> None:
        if not self.can_refresh:
            raise RuntimeError(
                "Для автообновления токена нужны GOOGLE_REFRESH_TOKEN, GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET"
            )

        response = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=20,
        )
        if not response.ok:
            raise RuntimeError(
                f"Не удалось обновить Google access token: {response.status_code} {response.text}"
            )

        refreshed_token = response.json().get("access_token")
        if not refreshed_token:
            raise RuntimeError("Google token endpoint не вернул access_token")
        self._access_token = refreshed_token

    def headers(self) -> dict[str, str]:
        self.ensure_access_token()
        return google_headers(self._access_token)


def google_request(
    method: str,
    auth: GoogleCalendarAuth,
    url: str,
    *,
    params: dict | None = None,
    json: dict | None = None,
) -> requests.Response:
    response = requests.request(
        method,
        url,
        headers=auth.headers(),
        params=params,
        json=json,
        timeout=20,
    )
    if response.status_code == 401 and auth.can_refresh:
        auth.refresh_access_token()
        response = requests.request(
            method,
            url,
            headers=auth.headers(),
            params=params,
            json=json,
            timeout=20,
        )
    return response


def get_existing_hse_event_ids(
    auth: GoogleCalendarAuth,
    calendar_id: str,
    time_min: str,
    time_max: str,
) -> Set[str]:
    existing_ids: Set[str] = set()
    page_token = None

    while True:
        params = {
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "maxResults": 2500,
        }
        if page_token:
            params["pageToken"] = page_token

        response = google_request(
            "GET",
            auth,
            google_events_url(calendar_id),
            params=params,
        )
        if not response.ok:
            raise RuntimeError(
                f"Не удалось получить события Google Calendar: {response.status_code} {response.text}"
            )

        payload = response.json()
        for event in payload.get("items", []):
            hse_id = (
                event.get("extendedProperties", {})
                .get("private", {})
                .get("hse_lesson_id")
            )
            if hse_id:
                existing_ids.add(hse_id)

        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    return existing_ids


def build_event(lesson: Lesson, timezone: str) -> dict:
    lecturers = ", ".join(
        profile.full_name for profile in lesson.lecturer_profiles if profile.full_name
    )
    stream_links = (
        "\n".join(link.link for link in (lesson.stream_links or []) if link.link) or "-"
    )
    description_parts = [
        f"Тип: {lesson.type}",
        f"Поток: {lesson.stream}",
        f"Преподаватели: {lecturers or '-'}",
        f"Заметка: {lesson.note or '-'}",
        f"Ссылки: {stream_links}",
    ]
    location = ", ".join(part for part in [lesson.building, lesson.auditorium] if part)

    return {
        "summary": lesson.discipline,
        "location": location,
        "description": "\n".join(description_parts),
        "start": {
            "dateTime": lesson.date_start.isoformat(),
            "timeZone": timezone,
        },
        "end": {
            "dateTime": lesson.date_end.isoformat(),
            "timeZone": timezone,
        },
        "extendedProperties": {
            "private": {
                "hse_lesson_id": lesson.id,
            }
        },
    }


def insert_events(
    auth: GoogleCalendarAuth,
    calendar_id: str,
    events: Iterable[dict],
    dry_run: bool,
) -> int:
    created = 0
    for event in events:
        if dry_run:
            print(
                f"[DRY-RUN] {event['start']['dateTime']} -> {event['summary']} ({event.get('location', '-')})"
            )
            created += 1
            continue

        response = google_request(
            "POST",
            auth,
            google_events_url(calendar_id),
            json=event,
        )
        if not response.ok:
            raise RuntimeError(
                f"Не удалось создать событие: {response.status_code} {response.text}"
            )
        created += 1
    return created


def main() -> None:
    load_dotenv()
    args = parse_args()

    if args.days <= 0:
        raise ValueError("--days должен быть больше 0")

    email = os.environ.get("email")
    password = os.environ.get("password")
    google_access_token = os.environ.get("GOOGLE_ACCESS_TOKEN")
    google_refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")
    google_client_id = os.environ.get("GOOGLE_CLIENT_ID")
    google_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not email or not password:
        raise RuntimeError("В .env должны быть заданы email и password")
    if not google_access_token and not google_refresh_token:
        raise RuntimeError(
            "В .env должна быть задана GOOGLE_ACCESS_TOKEN или связка GOOGLE_REFRESH_TOKEN + GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET"
        )

    auth = GoogleCalendarAuth(
        access_token=google_access_token,
        refresh_token=google_refresh_token,
        client_id=google_client_id,
        client_secret=google_client_secret,
    )

    start_date = parse_start_date(args.start_date)
    lessons = collect_lessons(Account.auth(email, password), start_date, args.days)

    if not lessons:
        print("Занятий за выбранный период не найдено")
        return

    period_start = min(lesson.date_start for lesson in lessons).isoformat()
    period_end = max(lesson.date_end for lesson in lessons).isoformat()

    existing_event_ids = get_existing_hse_event_ids(
        auth,
        args.calendar_id,
        period_start,
        period_end,
    )
    new_lessons = [lesson for lesson in lessons if lesson.id not in existing_event_ids]

    if not new_lessons:
        print("Все занятия уже есть в календаре, новых событий нет")
        return

    events = [build_event(lesson, args.timezone) for lesson in new_lessons]
    created = insert_events(
        auth,
        args.calendar_id,
        events,
        args.dry_run,
    )
    mode = "планируется к добавлению" if args.dry_run else "добавлено"
    print(f"Готово: {created} событий {mode}")


if __name__ == "__main__":
    main()
