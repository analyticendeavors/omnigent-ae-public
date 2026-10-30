"""Seed the Global Policies page from the server config, once.

Until 2026-10-18 the AE policies sat under the server config's ``policies:``
key. Upstream loads that key into ``RuntimeCaps.default_policies`` at boot and
shows the entries read-only on Settings > Global Policies: no switch, no
delete, no edit. The owner asked to manage them on that page and chose (2026-10-18)
to let the page own them, so they now live in the policy store as ordinary
global policies: the page's switch, trash and Edit buttons (Edit from
patch 0095) work on them, and a change made there survives deploys.

``ae_seed_policies:`` (the same grammar as ``policies:``) carries them now.
When the container builds the app (:mod:`ae_omni_policies.docker_entrypoint`),
the store gets every entry inserted, in order, unless it already holds one of
them, matched by name or by handler so a policy renamed on the page still
counts. A policy deleted on the page therefore stays deleted while any other
seeded one remains; deleting every seeded policy brings the seed back at the
next restart. Global policies made on the page with other handlers play no
part.

Before anything is written, each entry is checked the way the page's own
create route checks a policy, plus one step: the factory is built once.
Upstream's schema check covers key names and JSON types only, while the AE
factories reject bad values (``mode: nope``) with ``ValueError`` when built,
and a stored policy that cannot build breaks policy evaluation for every
session. The check of the store and the inserts share one transaction, so a
failure or a killed container leaves no partial seed for the next boot to
mistake for a finished one. A failure raises, the container exits, ``/health``
fails, and the auto-update rolls back to the previous image instead of booting
without the slot cap.
"""

from __future__ import annotations

import importlib
import json
import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from omnigent.db.db_models import SqlPolicy, current_workspace_id
from omnigent.db.enum_codecs import encode_policy_scope, encode_policy_type
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.policies.function import resolve_function_policy
from omnigent.policies.registry import (
    is_registered_handler,
    load_registry,
    validate_factory_params,
)
from omnigent.spec import parse_default_policies
from omnigent.spec.types import FunctionPolicySpec, FunctionRef
from omnigent.stores.policy_store import PolicyStore
from sqlalchemy import select
from sqlalchemy.orm import Session

_logger = logging.getLogger(__name__)

SEED_KEY = "ae_seed_policies"
# The id's first byte carries the entry's position (see seed_ids).
MAX_SEED_ENTRIES = 256
_FIX = "Fix it in config/server.yaml."


class SeedError(RuntimeError):
    """An ``ae_seed_policies`` entry the policy store cannot hold or build."""


@dataclass(frozen=True)
class SeedPolicy:
    """One ``ae_seed_policies`` entry, in the shape a global policy row takes.

    :param name: The policy name, the entry's key in the config.
    :param handler: Dotted path of the callable or factory.
    :param factory_params: Factory keyword arguments, or ``None`` for a direct
        callable or a factory left on its defaults.
    """

    name: str
    handler: str
    factory_params: dict[str, Any] | None


def parse_seed(cfg: Mapping[str, Any]) -> list[SeedPolicy]:
    """Read ``ae_seed_policies`` from the server config, in config order.

    :param cfg: The parsed server config.
    :returns: The entries, or ``[]`` when the key is absent or empty.
    :raises SeedError: When an entry is not a function policy, or carries
        ``set_labels`` or ``config``, which a global policy row cannot hold.
    """
    raw = cfg.get(SEED_KEY)
    if not raw:
        return []
    if not isinstance(raw, Mapping):
        raise SeedError(
            f"{SEED_KEY} must be a mapping of policy name to policy, "
            f"got {type(raw).__name__}. {_FIX}"
        )
    entries: list[SeedPolicy] = []
    for spec in parse_default_policies(dict(raw)):
        if not isinstance(spec, FunctionPolicySpec) or spec.function is None:
            raise SeedError(
                f"{SEED_KEY}: {spec.name!r} is not a function policy. The Global "
                "Policies page stores python handlers only; move it to `policies:`."
            )
        if spec.set_labels or spec.config:
            raise SeedError(
                f"{SEED_KEY}: {spec.name!r} sets `set_labels` or `config`, which a "
                "global policy row cannot hold. Drop them or move it to `policies:`."
            )
        params = spec.function.arguments
        entries.append(
            SeedPolicy(
                name=spec.name,
                handler=spec.function.path,
                factory_params=dict(params) if params is not None else None,
            )
        )
    return entries


def check_seed(entries: Sequence[SeedPolicy], policy_modules: Sequence[str] | None) -> None:
    """Check every entry the way the create route does, then build its factory.

    :param entries: The parsed entries.
    :param policy_modules: The server config's ``policy_modules``, scanned into
        the registry first so the AE handlers are known.
    :raises SeedError: Naming the first entry that fails and why.
    """
    modules = list(policy_modules or [])
    for module in modules:
        # load_registry logs an import failure and moves on, which would surface
        # below as an unregistered handler and point at the wrong fix.
        try:
            importlib.import_module(module)
        except ImportError as exc:
            raise SeedError(
                f"policy_modules: {module!r} does not import ({exc}). Fix the image's "
                "install of that package, or the module path in config/server.yaml."
            ) from exc
    load_registry(extra_modules=modules)
    for entry in entries:
        if not is_registered_handler(entry.handler):
            raise SeedError(
                f"{SEED_KEY}: {entry.name!r} names {entry.handler!r}, which no module "
                "in policy_modules registers. Fix the path or add its module."
            )
        problem = validate_factory_params(entry.handler, entry.factory_params)
        if problem is not None:
            raise SeedError(f"{SEED_KEY}: {entry.name!r}: {problem}. {_FIX}")
        spec = FunctionPolicySpec(
            name=entry.name,
            on=None,
            function=FunctionRef(path=entry.handler, arguments=entry.factory_params),
        )
        try:
            resolve_function_policy(spec)
        except Exception as exc:
            raise SeedError(
                f"{SEED_KEY}: {entry.name!r} does not build with these params ({exc}). {_FIX}"
            ) from exc


def seed_ids(count: int) -> list[str]:
    """Policy ids that keep the entries in config order on the page.

    ``list_defaults`` sorts by ``created_at`` (whole seconds), then by id, so
    a random id would shuffle entries inserted in the same second.

    :param count: How many ids.
    :returns: 32-char hex ids whose first byte is the entry's position.
    """
    if count > MAX_SEED_ENTRIES:
        raise SeedError(
            f"{SEED_KEY} holds {count} entries; the limit is {MAX_SEED_ENTRIES}. "
            "Move the rest to the Global Policies page."
        )
    return [f"{index:02x}{uuid.uuid4().hex[2:]}" for index in range(count)]


@dataclass(frozen=True)
class _SeedRun:
    """What one transaction found and did."""

    inserted: list[str]
    present: list[str]


def _seeded(entries: Sequence[SeedPolicy], rows: Iterable[tuple[str, str]]) -> list[str]:
    """Names of the global policies that match a seed entry by name or handler.

    :param entries: The seed.
    :param rows: ``(name, handler)`` of every global policy in the store.
    :returns: The matching policies' names, in store order.
    """
    names = {entry.name for entry in entries}
    handlers = {entry.handler for entry in entries}
    return [name for name, handler in rows if name in names or handler in handlers]


def _insert_seed(store: PolicyStore, entries: Sequence[SeedPolicy]) -> _SeedRun:
    """Insert every entry in one transaction unless the store holds one already.

    Built on the store's own engine and row shape (``create_default``), since
    the store commits each ``create_default`` call on its own.

    :param store: The server's policy store.
    :param entries: The checked entries.
    :returns: The inserted ids, or the names of the global policies that
        matched a seed entry by name or handler (then nothing is inserted).
    """
    maker = make_named_managed_session_maker(
        get_or_create_engine(store.storage_location),
        query_name_prefix="ae_omni_policies.policy_seed",
        immediate=True,
    )
    ids = seed_ids(len(entries))

    def write(session: Session) -> _SeedRun:
        rows = session.execute(
            select(SqlPolicy.name, SqlPolicy.handler)
            .where(SqlPolicy.workspace_id == current_workspace_id())
            .where(SqlPolicy.scope == encode_policy_scope("default"))
        ).all()
        present = _seeded(entries, ((name, handler) for name, handler in rows))
        if present:
            return _SeedRun(inserted=[], present=present)
        created_at = now_epoch()
        for policy_id, entry in zip(ids, entries, strict=True):
            session.add(
                SqlPolicy(
                    id=policy_id,
                    name=entry.name,
                    session_id=None,
                    scope=encode_policy_scope("default"),
                    created_at=created_at,
                    updated_at=None,
                    type=encode_policy_type("python"),
                    handler=entry.handler,
                    factory_params=json.dumps(entry.factory_params)
                    if entry.factory_params
                    else None,
                    enabled=True,
                    created_by=None,
                )
            )
        session.flush()
        return _SeedRun(inserted=ids, present=[])

    return run_write_transaction(maker, "seed_default_policies", write)


def seed_policies(
    store: PolicyStore,
    cfg: Mapping[str, Any],
    policy_modules: Sequence[str] | None = None,
) -> list[str]:
    """Insert the seed as global policies unless the store holds one of them.

    :param store: The server's policy store, after migrations.
    :param cfg: The parsed server config.
    :param policy_modules: The server config's ``policy_modules``.
    :returns: The inserted policy ids, or ``[]`` when nothing was seeded.
    :raises SeedError: When an entry fails its checks, or shares a name or a
        handler with an entry under ``policies:`` (it would run twice).
    """
    entries = parse_seed(cfg)
    if not entries:
        return []
    _refuse_double_runs(cfg, entries)
    present = _seeded(entries, ((p.name, p.handler) for p in store.list_defaults()))
    if not present:
        check_seed(entries, policy_modules)
        run = _insert_seed(store, entries)
        if run.inserted:
            for policy_id, entry in zip(run.inserted, entries, strict=True):
                _logger.info("%s: seeded %s as global policy %s", SEED_KEY, entry.name, policy_id)
            return run.inserted
        present = run.present
    missing = [entry.name for entry in entries if entry.name not in present]
    _logger.info(
        "%s: the policy store already holds %s; nothing seeded.%s",
        SEED_KEY,
        ", ".join(present),
        f" Not in the store under these names: {', '.join(missing)}." if missing else "",
    )
    return []


def _refuse_double_runs(cfg: Mapping[str, Any], entries: Sequence[SeedPolicy]) -> None:
    """Refuse a seed entry that ``policies:`` also declares, by name or handler.

    Upstream runs ``policies:`` from the image and the store's rows as well, so
    such an entry would run twice on every prompt.

    :param cfg: The parsed server config.
    :param entries: The seed.
    :raises SeedError: Naming the entries declared twice.
    """
    declared = cfg.get("policies")
    if not isinstance(declared, Mapping) or not declared:
        return
    specs = parse_default_policies(dict(declared))
    names = {spec.name for spec in specs}
    handlers = {
        spec.function.path
        for spec in specs
        if isinstance(spec, FunctionPolicySpec) and spec.function is not None
    }
    both = sorted(
        entry.name for entry in entries if entry.name in names or entry.handler in handlers
    )
    if both:
        raise SeedError(
            f"{', '.join(both)} also sit under `policies:`, by name or by handler; each "
            f"would run twice. Keep them under {SEED_KEY} only in config/server.yaml."
        )
