"""Every secret in both Key Vaults, for the Admin Center's Key Vault card (2026-10-23).

Why: the Box health card answers "does this box have what it needs" for the 17
keys the box pulls. Reid also wants the whole inventory: every secret across
``ae-secrets-vault`` and the website's vault, which ones expire, which have not
changed in half a year, and which live in both vaults and so must be rotated
twice (the global CLAUDE.md names two). The concept page
(``secrets-vault-concept.html``, approved 2026-10-23) laid out the card.

The data is the box relay's daily reading (``secrets_meta.py``,
``secrets_relay.py``): names, enabled, expires, updated and the source tag, never
a value. ``GET /v1/ae/vault`` (admin only, router.py) answers :func:`view`.

"Changed" is when the current version was made (``SecretMeta.changed``), so
setting a tag or an expiry is not a change (review of PR 301, 2026-10-23).

Groups are the first word of each name ("databricks-mvp-client-id" groups under
Databricks), so ``ae-`` and ``hc-`` group by company rather than service. The
concept accepted that to start; :data:`LABELS` only spells the group names.

"Untouched" is 180 days without a change. Only 2 of 103 secrets carry an expiry
today, so that count carries the signal until more dates are set.

Both vaults: a secret whose two copies changed more than a day apart reads as
"Rotate in both vaults" in the needs-you list, because one copy was rotated and
the other was not (acs-email-connection-string and hubspot-access-token,
2026-10-23).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from ae_omni_policies import secret_alerts, secret_owners, secrets_meta

UNTOUCHED_DAYS = 180
NEEDS_YOU_SHOWN = 6
BOTH_VAULTS_SLACK_S = 24 * 3600
"""Two copies changed within a day of each other count as rotated together."""

# Group spellings; anything else is the first word, capitalised.
LABELS: dict[str, str] = {
    "acs": "Azure email",
    "ae": "AE",
    "cf": "Cloudflare Access",
    "devops": "Azure DevOps",
    "fabconlinkid": "Fabric",
    "freshbooks": "FreshBooks",
    "github": "GitHub",
    "hc": "HC",
    "hubspot": "HubSpot",
    "mcpo": "MCPO",
    "omnigent": "OmniGent",
    "youtube": "YouTube",
}

NeedKind = Literal["expired", "soon", "disabled", "both", "untouched"]


class VaultSecret(BaseModel):
    name: str
    group: str
    enabled: bool
    updated: str | None
    updated_days: int | None
    """Whole days since the last change."""
    expires: str | None
    vault_expires: str | None
    source_expires: str | None
    expiry_from: secrets_meta.ExpiryFrom | None
    days_left: int | None
    expiry_state: secrets_meta.ExpiryState
    untouched: bool
    on_box: bool
    box_key: str | None
    """The env key the box pulls this secret into, when it does."""
    both_vaults: bool
    rotate_url: str | None = None
    """The issuer's rotate page; ``None`` when it is the vault's own, which the page
    builds itself. Rows leave out ``edit_url`` and the default hint for the same reason:
    with them the answer was 91 KB for 116 rows, over the extension bridge's 64 KiB cap
    (the card read "Server response exceeds the limit", 2026-10-23, PR 304)."""
    rotate_hint: str | None = None
    owner: str | None = None
    """The OmniGent user who owns it (``secret_owners.py``); always sent, ``None`` for none."""
    alert: secret_alerts.AlertInfo | None = None
    """Its open expiry alert task (``secret_alerts.py``), if any."""


class VaultGroup(BaseModel):
    id: str
    label: str
    count: int
    oldest_days: int | None
    """The longest any secret in the group has gone unchanged."""


class Counts(BaseModel):
    expired: int = 0
    soon: int = 0
    no_expiry: int = 0
    untouched: int = 0
    no_owner: int = 0


class Need(BaseModel):
    name: str
    kind: NeedKind
    text: str
    owner: str | None = None
    alarm_at: str | None = None


class VaultView(BaseModel):
    vault: str
    read_from: str | None = None
    """When this vault was last read, if the newest reading could not read it."""
    count: int
    counts: Counts
    needs_you: list[Need]
    """The first :data:`NEEDS_YOU_SHOWN`, worst first."""
    needs_you_total: int
    groups: list[VaultGroup]
    secrets: list[VaultSecret]


class Inventory(BaseModel):
    read_at: str | None
    stale: bool
    box_vault: str
    vaults: list[VaultView]
    owners_editable: bool = False
    """Whether an owner set from the card survives a restart."""
    alerts: secret_alerts.Summary | None = None


def group_of(name: str) -> str:
    return name.split("-", 1)[0].lower() or name


def label_of(group: str) -> str:
    return LABELS.get(group, group[:1].upper() + group[1:])


_BOX_KEY = {
    name: key
    for key, name in (*secrets_meta.VAULT_NAMES.items(), *secrets_meta.HOST_VAULT_NAMES.items())
}

_NEED_ORDER: dict[NeedKind, int] = {
    "expired": 0,
    "soon": 1,
    "disabled": 2,
    "both": 3,
    "untouched": 4,
}


def _days_text(days: int) -> str:
    return f"{days} day" if days == 1 else f"{days} days"


def _rows(
    vault: str,
    secrets: dict[str, secrets_meta.SecretMeta],
    twins: dict[str, secrets_meta.SecretMeta],
    *,
    box: bool,
    now: datetime,
    entra_from: datetime | None = None,
    owners: Mapping[str, str],
    alerts: Mapping[str, secret_alerts.AlertInfo],
) -> tuple[list[VaultSecret], list[Need]]:
    rows: list[VaultSecret] = []
    needs: list[tuple[int, int, Need]] = []
    for name in sorted(secrets):
        meta = secrets[name]
        # Only the box vault's copy is the one the server holds, so only it may
        # take the server's GitHub expiry; an Entra tag counts in either vault.
        words = secrets_meta.expiry(
            meta,
            now,
            source=box or name != secrets_meta.GITHUB_VAULT_NAME,
            entra_from=entra_from,
        )
        changed = meta.changed
        age = (now - changed).days if changed else None
        untouched = age is not None and age >= UNTOUCHED_DAYS
        on_box = box and name in secrets_meta.BOX_NAMES
        twin = twins.get(name)
        rotate_url, rotate_hint = secrets_meta.rotate_link(meta, vault)
        own_page = rotate_url == secrets_meta.edit_url(vault, name)
        key = secret_owners.key_of(vault, name)
        row = VaultSecret(
            name=name,
            group=group_of(name),
            enabled=meta.enabled,
            updated=changed.isoformat() if changed else None,
            updated_days=age,
            untouched=untouched,
            on_box=on_box,
            box_key=_BOX_KEY.get(name) if on_box else None,
            both_vaults=twin is not None,
            rotate_url=None if own_page else rotate_url,
            rotate_hint=None if own_page else rotate_hint,
            owner=owners.get(key),
            alert=alerts.get(key),
            **words.model_dump(),
        )
        rows.append(row)
        need: Need | None = None
        if row.expiry_state == "expired":
            need = Need(name=name, kind="expired", text=f"Expired {(row.expires or '')[:10]}")
        elif row.expiry_state == "soon":
            origin = {"entra": ", from Entra", "github": ", from GitHub"}.get(
                row.expiry_from or "", ""
            )
            need = Need(
                name=name,
                kind="soon",
                text=f"Expires in {_days_text(row.days_left or 0)}{origin}",
            )
        elif not meta.enabled and on_box:
            need = Need(
                name=name, kind="disabled", text="Disabled in Key Vault, and the box uses it"
            )
        elif twin is not None and _apart(meta, twin):
            need = Need(name=name, kind="both", text="Rotate in both vaults")
        elif untouched and row.expiry_state == "none":
            need = Need(
                name=name,
                kind="untouched",
                text=f"Not changed in {_days_text(age or 0)}, no expiry set",
            )
        if need is not None:
            need.owner = row.owner
            need.alarm_at = row.alert.alarm_at if row.alert else None
            # Worst kind first, then the soonest expiry or the oldest change.
            rank = row.days_left if need.kind in ("expired", "soon") else -(age or 0)
            needs.append((_NEED_ORDER[need.kind], rank or 0, need))
    needs.sort(key=lambda item: (item[0], item[1], item[2].name))
    return rows, [need for _, _, need in needs]


def _apart(one: secrets_meta.SecretMeta, two: secrets_meta.SecretMeta) -> bool:
    if one.changed is None or two.changed is None:
        return False
    return abs((one.changed - two.changed).total_seconds()) > BOTH_VAULTS_SLACK_S


def _view(
    vault: str, rows: list[VaultSecret], needs: list[Need], read_from: datetime | None = None
) -> VaultView:
    counts = Counts(
        expired=sum(row.expiry_state == "expired" for row in rows),
        soon=sum(row.expiry_state == "soon" for row in rows),
        no_expiry=sum(row.expiry_state == "none" for row in rows),
        untouched=sum(row.untouched for row in rows),
        no_owner=sum(row.owner is None for row in rows),
    )
    groups: dict[str, list[VaultSecret]] = {}
    for row in rows:
        groups.setdefault(row.group, []).append(row)
    return VaultView(
        vault=vault,
        read_from=read_from.isoformat() if read_from else None,
        count=len(rows),
        counts=counts,
        needs_you=needs[:NEEDS_YOU_SHOWN],
        needs_you_total=len(needs),
        groups=[
            VaultGroup(
                id=group,
                label=label_of(group),
                count=len(members),
                oldest_days=max(
                    (row.updated_days for row in members if row.updated_days is not None),
                    default=None,
                ),
            )
            for group, members in sorted(
                groups.items(), key=lambda item: label_of(item[0]).lower()
            )
        ],
        secrets=rows,
    )


def view(
    reading: secrets_meta.Reading | None,
    now: datetime,
    *,
    owners: Mapping[str, str] | None = None,
    alerts: Mapping[str, secret_alerts.AlertInfo] | None = None,
) -> Inventory:
    """The card's answer: each vault with its counts, needs-you list, groups and rows.

    :param owners: Owner by ``<vault>/<name>`` (``secret_owners.py``).
    :param alerts: Open alert by ``<vault>/<name>`` (``secret_alerts.infos``).
    """
    if reading is None:
        return Inventory(read_at=None, stale=True, box_vault=secrets_meta.VAULT, vaults=[])
    vaults = [(reading.vault, reading.secrets, True)] + [
        (name, secrets, False) for name, secrets in reading.vaults.items()
    ]
    out: list[VaultView] = []
    for name, secrets, box in vaults:
        twins: dict[str, secrets_meta.SecretMeta] = {}
        for other, other_secrets, _ in vaults:
            if other == name:
                continue
            for secret_name, meta in other_secrets.items():
                if secret_name in secrets:
                    twins.setdefault(secret_name, meta)
        rows, needs = _rows(
            name,
            secrets,
            twins,
            box=box,
            now=now,
            entra_from=reading.carried("entra"),
            owners=owners or {},
            alerts=alerts or {},
        )
        out.append(_view(name, rows, needs, reading.carried(f"vault:{name}")))
    return Inventory(
        read_at=reading.read_at.isoformat(),
        stale=secrets_meta.stale(reading, now),
        box_vault=reading.vault,
        vaults=out,
    )


__all__ = [
    "LABELS",
    "NEEDS_YOU_SHOWN",
    "UNTOUCHED_DAYS",
    "Counts",
    "Inventory",
    "Need",
    "VaultGroup",
    "VaultSecret",
    "VaultView",
    "group_of",
    "label_of",
    "view",
]
