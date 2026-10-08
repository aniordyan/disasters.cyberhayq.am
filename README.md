# CyberHayq category mover

This script moves items between category tabs in the CyberHayq disaster platform.
It reads a user page URL, user UUID, or role/category UUID, finds pins whose names
contain chosen words, previews the matches, and only writes changes when `--apply`
is used.

## Recommended workflow

First run a dry run:

```bash
python3 move_pins.py \
  --uuid "https://disasters.cyberhayq.am/db/get?id=...#..." \
  --source "Random Pins" \
  --target Transport \
  --keyword train
```

If the preview is correct, apply it:

```bash
python3 move_pins.py \
  --uuid "https://disasters.cyberhayq.am/db/get?id=...#..." \
  --source "Random Pins" \
  --target Transport \
  --keyword train \
  --apply
```

The script will ask you to type `MOVE` before writing. Add `--interactive` if you
want to approve each matching item one by one.

You can also run it with no arguments and answer the prompts:

```bash
python3 move_pins.py
```

## Move between any categories

Use `--source` for the origin category and `--target` for the destination:

```bash
python3 move_pins.py --uuid "<page-url-or-uuid>" --source Telecom --target "Service Providers" --keyword Ucom
python3 move_pins.py --uuid "<page-url-or-uuid>" --source Hospitals --target "Random Pins" --keyword clinic
python3 move_pins.py --uuid "<page-url-or-uuid>" --source Food --target Financial --keyword bank
```

## Notes

- If the UUID belongs to a user page, the script uses that user's first role by
  default. Use `--role-index 2` or `--role-name "Citizen of Tavush"` when needed.
- Use `--keyword` more than once for multiple words.
- Use `--match all` when every keyword must be present.
- Use `--search all` to search every text field instead of only the name.
- Type conversion is automatic by default because the website stores tabs as
  different item types. Use `--no-convert-type` to skip items that would need
  conversion.
- Before writing, the script saves JSON backups in `backups/<timestamp>/`.
