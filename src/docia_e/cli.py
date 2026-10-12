"""Comandos reales de la aplicación. No imprime secretos."""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from docia_e.adapters.extraction.service import ocr_status, package_version
from docia_e.adapters.jobs.worker import run_worker
from docia_e.adapters.passwords import hash_password, new_service_secret
from docia_e.adapters.persistence.mongo_store import MongoGateway, connect, ensure_indexes
from docia_e.adapters.storage.gridfs_store import GridFSBlobStore
from docia_e.adapters.web.app import create_app
from docia_e.application.maintenance import copy_database, set_maintenance, verify_copy
from docia_e.application.support import StoreConflict, new_id
from docia_e.config import load_settings, require_secret
from docia_e.domain.policies import ROLES
from docia_e.domain.timeutil import now_utc_z


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docia-e")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-indexes")
    user = sub.add_parser("create-user")
    user.add_argument("--login", required=True)
    user.add_argument("--roles", required=True, help="separados por coma")
    user.add_argument("--centers", required=True, help="separados por coma")
    token = sub.add_parser("create-token")
    token.add_argument("--login", required=True)
    revoke = sub.add_parser("revoke-token")
    revoke.add_argument("--token-id", required=True)
    sub.add_parser("web")
    sub.add_parser("worker")
    sub.add_parser("diagnose")
    sub.add_parser("check-deps")
    maintenance = sub.add_parser("maintenance")
    maintenance.add_argument("--on", action="store_true")
    maintenance.add_argument("--off", action="store_true")
    maintenance.add_argument("--reason", default="copia")
    backup = sub.add_parser("backup")
    backup.add_argument("--target-db", required=True)
    restore = sub.add_parser("verify-restore")
    restore.add_argument("--target-db", required=True)
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--older-than", required=True)
    reconcile.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    settings = load_settings()
    if args.command in {"web"}:
        require_secret(settings)
    client = connect(settings.mongo_uri)
    gateway = MongoGateway(client, settings.mongo_db)
    blobs = GridFSBlobStore(client[settings.mongo_db], settings.gridfs_bucket)
    try:
        return _dispatch(args, settings, client, gateway, blobs)
    except Exception as exc:
        if args.command == "diagnose":
            print(f"mongo=down error={type(exc).__name__}")
            return 1
        print(f"error={type(exc).__name__}", file=sys.stderr)
        return 1


def _dispatch(args, settings, client, gateway, blobs) -> int:
    if args.command == "init-indexes":
        ensure_indexes(client[settings.mongo_db])
        print("indexes=ok")
        return 0
    if args.command == "create-user":
        roles = [role.strip() for role in args.roles.split(",") if role.strip()]
        unknown = [role for role in roles if role not in ROLES]
        if unknown:
            print("roles_no_reconocidos", file=sys.stderr)
            return 2
        password = os.environ.get("DOC_USER_PASSWORD") or getpass.getpass("Contraseña (no se mostrará): ")
        if len(password) < 10:
            print("contraseña_corta", file=sys.stderr)
            return 2
        user = {
            "id": new_id(),
            "login": args.login.strip(),
            "login_normalized": args.login.strip().lower(),
            "password_hash": hash_password(password),
            "roles": roles,
            "center_ids": [center.strip() for center in args.centers.split(",") if center.strip()],
            "active": True,
            "created_at": now_utc_z(),
        }
        try:
            gateway.store.insert_user(user)
        except StoreConflict:
            print("login_ya_existe", file=sys.stderr)
            return 2
        print(f"user_id={user['id']}")
        return 0
    if args.command == "create-token":
        user = gateway.store.get_user_by_login(args.login.strip().lower())
        if not user:
            print("usuario_no_encontrado", file=sys.stderr)
            return 2
        presented, token_id, digest = new_service_secret()
        gateway.store.insert_token(
            {
                "id": token_id,
                "user_id": user["id"],
                "secret_hash": digest,
                "active": True,
                "created_at": now_utc_z(),
            }
        )
        print("token_creado_copialo_ahora_no_se_vuelve_a_mostrar")
        print(presented)
        return 0
    if args.command == "revoke-token":
        gateway.store.revoke_token(args.token_id)
        print("token_revocado")
        return 0
    if args.command == "web":
        app = create_app(settings, gateway, blobs)
        from waitress import serve

        serve(app, host=settings.host, port=settings.port, threads=4)
        return 0
    if args.command == "worker":
        run_worker(gateway, blobs, settings)
        return 0
    if args.command == "diagnose":
        _print_diagnose(settings, gateway)
        return 0
    if args.command == "check-deps":
        _print_deps()
        return 0
    if args.command == "maintenance":
        if args.on == args.off:
            print("indica --on o --off", file=sys.stderr)
            return 2
        actor = {"id": "cli-admin", "roles": ["administrator"], "center_ids": [], "active": True}
        set_maintenance(gateway, actor, args.on, args.reason, now_utc_z)
        print("maintenance=" + ("on" if args.on else "off"))
        return 0
    if args.command == "backup":
        if not gateway.store.maintenance_enabled():
            print("activa_antes_maintenance_--on", file=sys.stderr)
            return 2
        counts = copy_database(client, settings.mongo_db, args.target_db, settings.gridfs_bucket)
        print("backup_counts=" + str({key: counts[key] for key in counts}))
        return 0
    if args.command == "verify-restore":
        report = verify_copy(client, settings.mongo_db, args.target_db, settings.gridfs_bucket)
        print("restore_ok=" + str(report["ok"]))
        if report["mismatches"]:
            print("mismatches=" + str(report["mismatches"]))
            return 1
        return 0
    if args.command == "reconcile":
        from docia_e.application.maintenance import reconcile

        actor = {"id": "cli-admin", "roles": ["administrator"], "center_ids": [], "active": True}
        report = reconcile(gateway, blobs, actor, args.older_than, args.apply)
        print("reconcile_apply=" + str(report["apply"]))
        for item in report["files"]:
            print(f"file_id={item['file_id']} state={item['state']} action={item['action']}")
        return 0
    return 2


def _print_diagnose(settings, gateway) -> None:
    ocr = ocr_status()
    print(f"python={sys.version.split()[0]}")
    print(f"mongo_db={settings.mongo_db}")
    print(f"profile={settings.document_profile}")
    print(f"max_upload_mib={settings.max_upload_mib} unit=MiB")
    print(f"ocr_available={ocr['available']} languages={','.join(ocr.get('languages') or [])}")
    try:
        topology = gateway.store.topology()
        print(f"replica_set={topology.get('set_name')} primary={topology.get('writable_primary')}")
    except Exception as exc:
        print(f"replica_set=unavailable error={type(exc).__name__}")
    if "directConnection=true" in settings.mongo_uri:
        print("aviso=directConnection oculta el replica set y puede impedir transacciones")
    heartbeat = gateway.store.latest_heartbeat()
    print("worker=" + ("seen" if heartbeat else "none"))
    _print_deps()


def _print_deps() -> None:
    for dist in ("flask", "pymongo", "pypdf", "python-docx", "pypdfium2", "pytesseract", "pillow", "waitress"):
        print(f"{dist}={package_version(dist)}")


if __name__ == "__main__":
    raise SystemExit(main())
