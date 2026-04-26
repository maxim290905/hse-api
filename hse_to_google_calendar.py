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


def get_existing_hse_event_ids(
    access_token: str,
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

        response = requests.get(
            google_events_url(calendar_id),
            headers=google_headers(access_token),
            params=params,
            timeout=20,
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
    access_token: str,
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

        response = requests.post(
            google_events_url(calendar_id),
            headers=google_headers(access_token),
            json=event,
            timeout=20,
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
    if not email or not password:
        raise RuntimeError("В .env должны быть заданы email и password")
    if not google_access_token:
        raise RuntimeError("В .env должна быть задана переменная GOOGLE_ACCESS_TOKEN")

    start_date = parse_start_date(args.start_date)
    lessons = collect_lessons(Account.auth(email, password), start_date, args.days)

    if not lessons:
        print("Занятий за выбранный период не найдено")
        return

    period_start = min(lesson.date_start for lesson in lessons).isoformat()
    period_end = max(lesson.date_end for lesson in lessons).isoformat()

    existing_event_ids = get_existing_hse_event_ids(
        google_access_token,
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
        google_access_token,
        args.calendar_id,
        events,
        args.dry_run,
    )
    mode = "планируется к добавлению" if args.dry_run else "добавлено"
    print(f"Готово: {created} событий {mode}")


if __name__ == "__main__":
    main()
