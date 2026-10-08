#!/usr/bin/env python3
"""
Move CyberHayq disaster-map objects between role category collections.

Default behavior is a dry run. Use --apply to write changes.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse


DEFAULT_BASE_URL = "https://disasters.cyberhayq.am"
UUID_PART = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
UUID_RE = re.compile(rf"{UUID_PART}(?:-{UUID_PART})*")


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    element_type: str


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def extract_uuid(value: str) -> str:
    text = value.strip()
    parsed = urlparse(text)
    candidates = [parsed.fragment, *parse_qs(parsed.query).get("id", []), text]
    for candidate in candidates:
        match = UUID_RE.search(candidate)
        if match:
            return match.group(0)
    raise SystemExit("Please provide a CyberHayq page URL or UUID.")


def localized_text(value: Any, preferred_locale: str = "en_US") -> str:
    if isinstance(value, dict):
        if preferred_locale in value:
            return str(value[preferred_locale])
        if value:
            return str(next(iter(value.values())))
        return ""
    return "" if value is None else str(value)


def flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        strings: list[str] = []
        for child in value.values():
            strings.extend(flatten_strings(child))
        return strings
    if isinstance(value, list):
        strings = []
        for child in value:
            strings.extend(flatten_strings(child))
        return strings
    return []


class MetaxClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        if shutil.which("curl") is None:
            raise RuntimeError("curl is required because this server rejects Python's built-in HTTP client.")

    def _curl(self, arguments: list[str], body: bytes | None = None) -> bytes:
        command = ["curl", "-fsSL", *arguments]
        completed = subprocess.run(
            command,
            input=body,
            capture_output=True,
            check=False,
            timeout=60,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"curl failed: {stderr or 'no error text returned'}")
        return completed.stdout

    def get_json(self, uuid: str) -> dict[str, Any]:
        url = urljoin(self.base_url, f"db/get?id={uuid}")
        try:
            raw = self._curl(["-H", "Accept: application/json", url])
            return json.loads(raw.decode("utf-8"))
        except (RuntimeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Failed to fetch {uuid}: {exc}") from exc

    def update_json(self, uuid: str, data: dict[str, Any]) -> None:
        url = urljoin(self.base_url, f"db/save/node?enc=0&id={uuid}")
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            raw = self._curl(
                [
                    "-X",
                    "POST",
                    "-H",
                    "Content-Type: application/json",
                    "-H",
                    "Authorization: Metax-Auth ",
                    "--data-binary",
                    "@-",
                    url,
                ],
                body=body,
            )
            payload = json.loads(raw.decode("utf-8"))
        except (RuntimeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Failed to update {uuid}: {exc}") from exc

        if payload.get("uuid") != uuid:
            raise RuntimeError(f"Update response for {uuid} was unexpected: {payload}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Move CyberHayq pins between category tabs by matching words."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--uuid",
        "--role-uuid",
        dest="uuid",
        help="Paste a CyberHayq page URL, user UUID, or role/category UUID.",
    )
    parser.add_argument(
        "--role-index",
        type=int,
        default=1,
        help="When a user UUID has multiple roles, choose this 1-based role number.",
    )
    parser.add_argument(
        "--role-name",
        help="When a user UUID has multiple roles, choose the role whose name contains this text.",
    )
    parser.add_argument("--source", help="Origin category, for example Random Pins or Transport.")
    parser.add_argument("--target", help="Destination category, for example Transport or Hospitals.")
    parser.add_argument(
        "--keyword",
        action="append",
        help="Word or phrase to match. Repeat for multiple keywords.",
    )
    parser.add_argument(
        "--match",
        choices=["any", "all"],
        default="any",
        help="Whether one keyword or every keyword must match.",
    )
    parser.add_argument(
        "--search",
        choices=["name", "all"],
        default="name",
        help="Search only the name field, or every string field in the object.",
    )
    parser.add_argument(
        "--convert-type",
        action="store_true",
        default=True,
        help="Change moved objects to the destination category item type. This is the default.",
    )
    parser.add_argument(
        "--no-convert-type",
        dest="convert_type",
        action="store_false",
        help="Only move objects that already have the destination category item type.",
    )
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument(
        "--fix-postals",
        action="store_true",
        help="Move items from the legacy hidden 'postals' field into the visible 'postal' field.",
    )
    parser.add_argument("--backup-dir", default="backups")
    return parser.parse_args()


def build_categories(role_type: dict[str, Any]) -> tuple[dict[str, Category], list[Category]]:
    categories: dict[str, Category] = {}
    ordered: list[Category] = []
    for collection in role_type.get("collections", []):
        key = collection.get("id")
        if not key:
            continue
        label = localized_text(collection.get("name")) or key
        category = Category(key=key, label=label, element_type=collection.get("element_type", ""))
        ordered.append(category)
        for alias in {key, label, key.replace("_", " "), label.rstrip("s")}:
            categories[normalize(alias)] = category

    # The live data has used both spellings at different times.
    if normalize("Postals") in categories:
        categories[normalize("postals")] = categories[normalize("Postals")]
    if normalize("Random Pins") in categories:
        categories[normalize("random_pins")] = categories[normalize("Random Pins")]

    return categories, ordered


def resolve_category(categories: dict[str, Category], value: str) -> Category:
    category = categories.get(normalize(value))
    if not category:
        available = ", ".join(sorted({c.label for c in categories.values()}))
        raise SystemExit(f"Unknown category '{value}'. Available categories: {available}")
    return category


def role_collection_keys(role: dict[str, Any], category: Category) -> list[str]:
    legacy_keys = {
        "postal": ["postals"],
    }
    keys = [category.key]
    for legacy_key in legacy_keys.get(category.key, []):
        if legacy_key in role:
            keys.append(legacy_key)
    return keys


def collection_values(role: dict[str, Any], keys: list[str]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for key in keys:
        for item_uuid in role.get(key, []):
            if item_uuid not in seen:
                values.append(item_uuid)
                seen.add(item_uuid)
    return values


def print_categories(categories: list[Category]) -> None:
    for index, category in enumerate(categories, start=1):
        print(f"  {index}. {category.label} ({category.key})")


def prompt_text(label: str, current: str | None = None, default: str | None = None) -> str:
    if current:
        return current
    suffix = f" [{default}]" if default else ""
    answer = input(f"{label}{suffix}: ").strip()
    if answer:
        return answer
    if default:
        return default
    raise SystemExit(f"{label} is required.")


def prompt_keywords(current: list[str] | None) -> list[str]:
    if current:
        return current
    answer = input("Keywords to match, separated by commas: ").strip()
    keywords = [part.strip() for part in answer.split(",") if part.strip()]
    if not keywords:
        raise SystemExit("At least one keyword is required.")
    return keywords


def choose_role_from_user(
    client: MetaxClient,
    user: dict[str, Any],
    role_index: int,
    role_name: str | None,
) -> tuple[str, dict[str, Any]]:
    role_ids = list(user.get("roles", []))
    if not role_ids:
        raise SystemExit("The provided user object does not contain any roles.")

    roles = [(role_id, client.get_json(role_id)) for role_id in role_ids]
    if role_name:
        needle = role_name.casefold()
        matches = [
            (role_id, role)
            for role_id, role in roles
            if needle in localized_text(role.get("name")).casefold()
        ]
        if not matches:
            raise SystemExit(f"No role name contains '{role_name}'.")
        if len(matches) > 1:
            print("Multiple roles matched:")
            for role_id, role in matches:
                print(f"  - {localized_text(role.get('name'))} [{role_id}]")
            raise SystemExit("Please use a more specific --role-name.")
        return matches[0]

    if len(roles) == 1:
        return roles[0]

    if role_index < 1 or role_index > len(roles):
        print("Available roles:")
        for index, (role_id, role) in enumerate(roles, start=1):
            print(f"  {index}. {localized_text(role.get('name'))} [{role_id}]")
        raise SystemExit(f"--role-index must be between 1 and {len(roles)}.")

    return roles[role_index - 1]


def resolve_role_object(
    client: MetaxClient,
    uuid_or_url: str,
    role_index: int,
    role_name: str | None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    input_uuid = extract_uuid(uuid_or_url)
    obj = client.get_json(input_uuid)
    obj_type = client.get_json(obj["type"])
    categories, _ = build_categories(obj_type)
    if normalize("Random Pins") in categories or normalize("Transport") in categories:
        return input_uuid, obj, obj_type

    if "roles" in obj:
        role_uuid, role = choose_role_from_user(client, obj, role_index, role_name)
        role_type = client.get_json(role["type"])
        return role_uuid, role, role_type

    raise SystemExit(
        "The provided UUID is not a role/category object and does not contain roles. "
        "Paste the page URL from the user's CyberHayq page or a role UUID."
    )


def searchable_text(item: dict[str, Any], mode: str) -> str:
    if mode == "name":
        return localized_text(item.get("name"))
    return " ".join(flatten_strings(item))


def keyword_matches(text: str, keywords: list[str], mode: str) -> bool:
    haystack = text.casefold()
    checks = [keyword.casefold() in haystack for keyword in keywords]
    return any(checks) if mode == "any" else all(checks)


def ensure_destination_shape(item: dict[str, Any], destination_type: dict[str, Any]) -> None:
    for collection in destination_type.get("collections", []):
        collection_id = collection.get("id")
        if collection_id and collection_id not in item:
            item[collection_id] = []


def save_backup(path: Path, name: str, data: dict[str, Any]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{name}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def ask_to_continue(count: int, source: Category, target: Category) -> None:
    print()
    print(f"Ready to move {count} item(s) from {source.label} to {target.label}.")
    answer = input("Type MOVE to continue: ").strip()
    if answer != "MOVE":
        raise SystemExit("Cancelled.")


def repair_postals(
    client: MetaxClient,
    role_uuid: str,
    role: dict[str, Any],
    apply_changes: bool,
    skip_confirmation: bool,
    backup_root: str,
) -> int:
    legacy_items = list(role.get("postals", []))
    visible_items = list(role.get("postal", []))
    missing_from_visible = [item_uuid for item_uuid in legacy_items if item_uuid not in visible_items]

    print("Postals repair")
    print(f"  legacy hidden field 'postals': {len(legacy_items)} item(s)")
    print(f"  visible field 'postal': {len(visible_items)} item(s)")

    if not missing_from_visible:
        print("Nothing to repair.")
        return 0

    print("Items that will be added to the visible Postals tab:")
    for item_uuid in missing_from_visible:
        try:
            item = client.get_json(item_uuid)
            item_name = localized_text(item.get("name")) or item_uuid
        except RuntimeError:
            item_name = item_uuid
        print(f"  - {item_name} [{item_uuid}]")

    if not apply_changes:
        print()
        print("Dry run only. Add --apply to repair the visible Postals tab.")
        return 0

    if not skip_confirmation:
        answer = input("Type REPAIR to continue: ").strip()
        if answer != "REPAIR":
            raise SystemExit("Cancelled.")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(backup_root) / timestamp
    save_backup(backup_dir, f"role-{role_uuid}", role)

    new_role = json.loads(json.dumps(role))
    new_role.setdefault("postal", [])
    for item_uuid in missing_from_visible:
        if item_uuid not in new_role["postal"]:
            new_role["postal"].append(item_uuid)
    new_role["postals"] = [item_uuid for item_uuid in new_role.get("postals", []) if item_uuid not in new_role["postal"]]

    client.update_json(role_uuid, new_role)
    print()
    print(f"Repaired Postals. Backup saved in {backup_dir}")
    return 0


def main() -> int:
    args = parse_args()
    client = MetaxClient(args.base_url)

    uuid_or_url = prompt_text("CyberHayq page URL or UUID", args.uuid)
    role_uuid, role, role_type = resolve_role_object(
        client,
        uuid_or_url,
        role_index=args.role_index,
        role_name=args.role_name,
    )
    categories, ordered_categories = build_categories(role_type)

    role_name = localized_text(role.get("name")) or role_uuid
    print(f"Role/category object: {role_name} [{role_uuid}]")

    if args.fix_postals:
        return repair_postals(
            client,
            role_uuid,
            role,
            apply_changes=args.apply,
            skip_confirmation=args.yes,
            backup_root=args.backup_dir,
        )

    print("Available categories:")
    print_categories(ordered_categories)
    print()

    source_name = prompt_text("Source category", args.source, default="Random Pins")
    target_name = prompt_text("Target category", args.target)
    keywords = prompt_keywords(args.keyword)

    source = resolve_category(categories, source_name)
    target = resolve_category(categories, target_name)
    if source.key == target.key:
        raise SystemExit("Source and target categories must be different.")
    source_storage_keys = role_collection_keys(role, source)
    target_storage_key = target.key
    destination_type = client.get_json(target.element_type)

    source_ids = collection_values(role, source_storage_keys)
    matches: list[tuple[str, str, dict[str, Any]]] = []
    skipped_wrong_type: list[tuple[str, str, str]] = []

    for item_uuid in source_ids:
        item = client.get_json(item_uuid)
        text = searchable_text(item, args.search)
        if not keyword_matches(text, keywords, args.match):
            continue

        item_name = localized_text(item.get("name")) or item_uuid
        if item.get("type") != target.element_type and not args.convert_type:
            skipped_wrong_type.append((item_uuid, item_name, item.get("type", "")))
            continue
        matches.append((item_uuid, item_name, item))

    print(f"Source: {source.label} ({', '.join(source_storage_keys)})")
    print(f"Target: {target.label} ({target_storage_key})")
    print(f"Keywords: {', '.join(keywords)}")
    print()

    if skipped_wrong_type:
        print("Matched but skipped because --no-convert-type was used:")
        for item_uuid, item_name, item_type in skipped_wrong_type:
            print(f"  - {item_name} [{item_uuid}] type={item_type}")
        print()

    if not matches:
        print("No movable matches found.")
        print("Available categories:")
        for category in ordered_categories:
            print(f"  - {category.label} ({category.key})")
        return 0

    selected: list[tuple[str, str, dict[str, Any]]] = []
    print("Movable matches:")
    for item_uuid, item_name, item in matches:
        suffix = " (will convert type)" if item.get("type") != target.element_type else ""
        print(f"  - {item_name} [{item_uuid}]{suffix}")
        if args.interactive and args.apply:
            answer = input("    Move this item? [y/N] ").strip().casefold()
            if answer == "y":
                selected.append((item_uuid, item_name, item))
        else:
            selected.append((item_uuid, item_name, item))

    if not selected:
        print("Nothing selected.")
        return 0

    if not args.apply:
        print()
        print("Dry run only. Add --apply to write changes.")
        return 0

    if not args.yes:
        ask_to_continue(len(selected), source, target)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(args.backup_dir) / timestamp
    save_backup(backup_dir, f"role-{role_uuid}", role)

    moved_ids = [item_uuid for item_uuid, _, _ in selected]
    original_items = {item_uuid: item for item_uuid, _, item in selected}
    for item_uuid, item in original_items.items():
        save_backup(backup_dir, f"item-{item_uuid}", item)

    new_role = json.loads(json.dumps(role))
    for source_storage_key in source_storage_keys:
        new_role.setdefault(source_storage_key, [])
    new_role.setdefault(target_storage_key, [])
    for source_storage_key in source_storage_keys:
        new_role[source_storage_key] = [
            item_uuid for item_uuid in new_role[source_storage_key] if item_uuid not in moved_ids
        ]
    for item_uuid in moved_ids:
        if item_uuid not in new_role[target_storage_key]:
            new_role[target_storage_key].append(item_uuid)

    updated_items: list[tuple[str, dict[str, Any]]] = []
    for item_uuid, _, item in selected:
        new_item = json.loads(json.dumps(item))
        if new_item.get("type") != target.element_type:
            new_item["type"] = target.element_type
            ensure_destination_shape(new_item, destination_type)
        updated_items.append((item_uuid, new_item))

    try:
        for item_uuid, item in updated_items:
            client.update_json(item_uuid, item)
        client.update_json(role_uuid, new_role)
    except Exception:
        print("Write failed. Attempting to restore changed items from backups...", file=sys.stderr)
        for item_uuid, item in original_items.items():
            try:
                client.update_json(item_uuid, item)
            except Exception as rollback_error:
                print(f"Rollback failed for {item_uuid}: {rollback_error}", file=sys.stderr)
        raise

    print()
    print(f"Moved {len(selected)} item(s). Backups saved in {backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
