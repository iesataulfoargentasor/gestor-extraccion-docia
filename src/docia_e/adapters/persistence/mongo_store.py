"""Catálogo MongoDB. Las sesiones de transacción entran por el constructor."""

from __future__ import annotations

import base64
import json

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from docia_e.application.support import StoreConflict


def connect(uri: str, server_selection_ms: int = 2000) -> MongoClient:
    return MongoClient(uri, serverSelectionTimeoutMS=server_selection_ms, tz_aware=True)


def ensure_indexes(db) -> None:
    db.users.create_index("login_normalized", unique=True)
    db.documents.create_index(
        [("center_id", ASCENDING), ("profile", ASCENDING), ("logical_key", ASCENDING)],
        unique=True,
        partialFilterExpression={"identity_resolved": True},
    )
    db.versions.create_index([("document_id", ASCENDING), ("ordinal", ASCENDING)], unique=True)
    db.versions.create_index([("document_id", ASCENDING), ("file_id", ASCENDING)], unique=True)
    db.versions.create_index([("center_id", ASCENDING), ("received_at", DESCENDING)])
    db.files.create_index(
        [("center_id", ASCENDING), ("purpose", ASCENDING), ("sha256", ASCENDING)],
        unique=True,
        partialFilterExpression={"purpose": "original"},
    )
    db.files.create_index(
        [("extraction_id", ASCENDING), ("artifact_kind", ASCENDING)],
        unique=True,
        partialFilterExpression={"purpose": "derivative"},
    )
    db.extractions.create_index(
        [("version_id", ASCENDING), ("config_fingerprint", ASCENDING), ("attempt", ASCENDING)],
        unique=True,
    )
    db.extraction_units.create_index([("extraction_id", ASCENDING), ("unit_id", ASCENDING)], unique=True)
    db.extraction_units.create_index([("extraction_id", ASCENDING), ("order", ASCENDING)])
    db.upload_requests.create_index([("actor_id", ASCENDING), ("idempotency_key", ASCENDING)], unique=True)
    db.jobs.create_index([("state", ASCENDING), ("lease_until", ASCENDING), ("created_at", ASCENDING)])
    db.jobs.create_index("request_key", unique=True)
    db.handoffs.create_index(
        "version_id",
        unique=True,
        partialFilterExpression={"status": "ready"},
    )
    db.handoffs.create_index([("center_id", ASCENDING), ("status", ASCENDING), ("created_at", ASCENDING)])
    db.change_events.create_index("sequence", unique=True)
    db.change_events.create_index([("center_id", ASCENDING), ("sequence", ASCENDING)])
    db.service_tokens.create_index("id", unique=True)


class MongoMetadataStore:
    def __init__(self, db, session=None) -> None:
        self.db = db
        self.session = session

    def maintenance_enabled(self) -> bool:
        doc = self.db.runtime_flags.find_one({"_id": "maintenance"}, session=self.session)
        return bool(doc and doc.get("enabled"))

    def set_maintenance(self, enabled: bool, reason: str, actor_id: str, at: str) -> None:
        self.db.runtime_flags.update_one(
            {"_id": "maintenance"},
            {"$set": {"enabled": enabled, "reason": reason, "actor_id": actor_id, "at": at}},
            upsert=True,
            session=self.session,
        )

    def get_upload(self, actor_id, key):
        return self._one("upload_requests", {"actor_id": actor_id, "idempotency_key": key})

    def insert_upload(self, doc):
        self._insert("upload_requests", doc, "upload_requests")

    def complete_upload(self, actor_id, key, response):
        self.db.upload_requests.update_one(
            {"actor_id": actor_id, "idempotency_key": key},
            {"$set": {"response": response}},
            session=self.session,
        )

    def delete_upload(self, actor_id, key):
        self.db.upload_requests.delete_one(
            {"actor_id": actor_id, "idempotency_key": key},
            session=self.session,
        )

    def find_original(self, center_id, sha256):
        return self._one("files", {"center_id": center_id, "purpose": "original", "sha256": sha256})

    def reserve_original(self, doc):
        try:
            self._insert("files", doc, "files_original")
            return {"outcome": "reserved", "file": doc}
        except StoreConflict:
            found = self.find_original(doc["center_id"], doc["sha256"])
            return {"outcome": "exists", "file": found}

    def update_file(self, file_id, expected_revision, owner_id, fields) -> bool:
        result = self.db.files.update_one(
            {"id": file_id, "revision": expected_revision, "owner_id": owner_id},
            {"$set": fields},
            session=self.session,
        )
        return result.modified_count == 1

    def update_file_unconditional(self, file_id, fields) -> None:
        self.db.files.update_one({"id": file_id}, {"$set": fields}, session=self.session)

    def get_file(self, file_id):
        return self._one("files", {"id": file_id})

    def insert_file(self, doc):
        self._insert("files", doc, "files")

    def find_document_by_logical_key(self, center_id, profile, logical_key):
        if not logical_key:
            return None
        return self._one(
            "documents",
            {"center_id": center_id, "profile": profile, "logical_key": logical_key, "identity_resolved": True},
        )

    def insert_document(self, doc):
        self._insert("documents", doc, "documents")

    def get_document(self, document_id):
        return self._one("documents", {"id": document_id})

    def update_document(self, document_id, expected_revision, fields) -> bool:
        result = self.db.documents.update_one(
            {"id": document_id, "revision": expected_revision},
            {"$set": fields},
            session=self.session,
        )
        return result.modified_count == 1

    def next_ordinal(self, document_id) -> int:
        last = self.db.versions.find_one(
            {"document_id": document_id},
            sort=[("ordinal", DESCENDING)],
            session=self.session,
        )
        return int(last["ordinal"]) + 1 if last else 1

    def insert_version(self, doc):
        self._insert("versions", doc, "versions")

    def get_version(self, version_id):
        return self._one("versions", {"id": version_id})

    def update_version(self, version_id, expected_metadata_revision, fields) -> bool:
        result = self.db.versions.update_one(
            {"id": version_id, "metadata_revision": expected_metadata_revision},
            {"$set": fields},
            session=self.session,
        )
        return result.modified_count == 1

    def list_versions(self, document_id):
        return self._list("versions", {"document_id": document_id}, [("ordinal", ASCENDING)])

    def versions_for_file(self, file_id):
        return self._list("versions", {"file_id": file_id}, [("received_at", ASCENDING)])

    def find_version_by_file(self, document_id, file_id):
        return self._one("versions", {"document_id": document_id, "file_id": file_id})

    def insert_source(self, doc):
        self._insert("sources", doc, "sources")

    def list_sources(self, version_id):
        return self._list("sources", {"version_id": version_id}, [("created_at", ASCENDING)])

    def insert_audit(self, doc):
        self._insert("audit_events", doc, "audit")

    def list_audit(self, document_id, limit):
        return self._list("audit_events", {"document_id": document_id}, [("at", DESCENDING)], limit)

    def insert_review(self, doc):
        self._insert("reviews", doc, "reviews")

    def list_reviews(self, subject_id):
        return self._list("reviews", {"subject_id": subject_id}, [("at", ASCENDING)])

    def insert_job(self, doc):
        self._insert("jobs", doc, "jobs")

    def get_job(self, job_id):
        return self._one("jobs", {"id": job_id})

    def get_job_by_key(self, request_key):
        return self._one("jobs", {"request_key": request_key})

    def claim_job(self, worker_id, now, lease_until):
        doc = self.db.jobs.find_one_and_update(
            {
                "$or": [
                    {"state": "queued"},
                    {"state": "running", "lease_until": {"$lte": now}},
                ]
            },
            {
                "$set": {"state": "running", "lease_owner": worker_id, "lease_until": lease_until, "started_at": now},
                "$inc": {"fencing_token": 1},
            },
            sort=[("created_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
            session=self.session,
        )
        return self._clean(doc)

    def renew_job(self, job_id, worker_id, token, lease_until) -> bool:
        result = self.db.jobs.update_one(
            {"id": job_id, "lease_owner": worker_id, "fencing_token": token},
            {"$set": {"lease_until": lease_until, "state": "running"}},
            session=self.session,
        )
        return result.modified_count == 1

    def finish_job(self, job_id, token, fields) -> bool:
        result = self.db.jobs.update_one(
            {"id": job_id, "fencing_token": token},
            {"$set": fields},
            session=self.session,
        )
        return result.modified_count == 1

    def release_job(self, job_id, token, tries, code) -> bool:
        result = self.db.jobs.update_one(
            {"id": job_id, "fencing_token": token},
            {"$set": {"state": "queued", "lease_owner": None, "lease_until": None, "tries": tries, "error_code": code}},
            session=self.session,
        )
        return result.modified_count == 1

    def heartbeat(self, doc):
        self.db.worker_heartbeats.update_one(
            {"_id": doc["worker_id"]},
            {"$set": doc},
            upsert=True,
            session=self.session,
        )

    def latest_heartbeat(self):
        doc = self.db.worker_heartbeats.find_one(sort=[("seen_at", DESCENDING)], session=self.session)
        return self._clean(doc)

    def insert_extraction(self, doc):
        self._insert("extractions", doc, "extractions")

    def get_extraction(self, extraction_id):
        return self._one("extractions", {"id": extraction_id})

    def update_extraction(self, extraction_id, fields):
        self.db.extractions.update_one({"id": extraction_id}, {"$set": fields}, session=self.session)

    def list_extractions(self, version_id):
        return self._list("extractions", {"version_id": version_id}, [("attempt", ASCENDING)])

    def find_extraction(self, version_id, fingerprint, attempt):
        return self._one(
            "extractions",
            {"version_id": version_id, "config_fingerprint": fingerprint, "attempt": attempt},
        )

    def find_text_fingerprint(self, center_id, fingerprint, exclude_version_id):
        if not fingerprint:
            return None
        return self._one(
            "versions",
            {"center_id": center_id, "text_fingerprint": fingerprint, "id": {"$ne": exclude_version_id}},
        )

    def replace_units(self, extraction_id, units):
        self.db.extraction_units.delete_many({"extraction_id": extraction_id}, session=self.session)
        if units:
            self.db.extraction_units.insert_many(units, session=self.session)

    def list_units(self, extraction_id, after_order, limit):
        query = {"extraction_id": extraction_id, "order": {"$gt": after_order}}
        return self._list("extraction_units", query, [("order", ASCENDING)], limit)

    def list_all_units(self, extraction_id):
        return self._list("extraction_units", {"extraction_id": extraction_id}, [("order", ASCENDING)], 5000)

    def insert_handoff(self, doc):
        self._insert("handoffs", doc, "handoffs_ready")

    def get_handoff(self, handoff_id):
        return self._one("handoffs", {"id": handoff_id})

    def update_handoff(self, handoff_id, fields):
        self.db.handoffs.update_one({"id": handoff_id}, {"$set": fields}, session=self.session)

    def list_ready_handoffs(self, center_ids, cursor, limit, family):
        query = {"status": "ready", "center_id": {"$in": list(center_ids)}}
        if family:
            query["snapshot.family"] = family
        if cursor:
            query["created_at"] = {"$gt": cursor}
        rows = self._list("handoffs", query, [("created_at", ASCENDING)], limit + 1)
        next_cursor = None
        if len(rows) > limit:
            next_cursor = rows[limit - 1]["created_at"]
            rows = rows[:limit]
        return rows, next_cursor

    def next_sequence(self) -> int:
        doc = self.db.counters.find_one_and_update(
            {"_id": "change_events"},
            {"$inc": {"value": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
            session=self.session,
        )
        return int(doc["value"])

    def insert_change(self, doc):
        self._insert("change_events", doc, "change_events")

    def list_changes(self, center_ids, after_sequence, limit):
        query = {"center_id": {"$in": list(center_ids)}}
        if after_sequence is not None:
            query["sequence"] = {"$gt": int(after_sequence)}
        return self._list("change_events", query, [("sequence", ASCENDING)], limit)

    def min_sequence(self, center_ids):
        doc = self.db.change_events.find_one(
            {"center_id": {"$in": list(center_ids)}},
            sort=[("sequence", ASCENDING)],
            session=self.session,
        )
        return int(doc["sequence"]) if doc else None

    def max_sequence(self, center_ids):
        doc = self.db.change_events.find_one(
            {"center_id": {"$in": list(center_ids)}},
            sort=[("sequence", DESCENDING)],
            session=self.session,
        )
        return int(doc["sequence"]) if doc else None

    def insert_discrepancy(self, doc):
        self._insert("discrepancies", doc, "discrepancies")

    def list_discrepancies(self, center_ids):
        return self._list("discrepancies", {"center_id": {"$in": list(center_ids)}, "status": "open"}, [("created_at", DESCENDING)])

    def search_versions(self, filters, cursor, limit):
        query = {"center_id": {"$in": filters["center_ids"]}}
        if not filters["reviewer"]:
            query["$or"] = [{"submitted_by": filters["actor_id"]}, {"handoff_status": "ready"}]
        for field in ("family", "academic_year", "documentary_state", "run_status", "handoff_status", "event_status"):
            if filters.get(field):
                query[field] = filters[field]
        if filters.get("audience"):
            query["audience"] = filters["audience"]
        if filters.get("received_from") or filters.get("received_to"):
            span = {}
            if filters.get("received_from"):
                span["$gte"] = filters["received_from"]
            if filters.get("received_to"):
                span["$lte"] = filters["received_to"]
            query["received_at"] = span
        if cursor:
            moment, version_id = _decode_cursor(cursor)
            if moment and version_id:
                query.setdefault("$and", []).append(
                    {"$or": [{"received_at": {"$lt": moment}}, {"received_at": moment, "id": {"$lt": version_id}}]}
                )
        rows = self._list("versions", query, [("received_at", DESCENDING), ("id", DESCENDING)], limit + 1)
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = _encode_cursor(last["received_at"], last["id"])
            rows = rows[:limit]
        return rows, next_cursor

    def counts(self, center_ids, actor_id, reviewer):
        base = {"center_id": {"$in": list(center_ids)}}
        if not reviewer:
            base = {"$and": [base, {"$or": [{"submitted_by": actor_id}, {"handoff_status": "ready"}]}]}
        versions = self._list("versions", base, None, 5000)
        jobs = self._list("jobs", {}, None, 5000) if reviewer else []
        by_doc = {}
        by_run = {}
        by_hand = {}
        for version in versions:
            by_doc[version.get("documentary_state") or "pending"] = by_doc.get(version.get("documentary_state") or "pending", 0) + 1
            by_run[version.get("run_status") or "none"] = by_run.get(version.get("run_status") or "none", 0) + 1
            by_hand[version.get("handoff_status") or "not_ready"] = by_hand.get(version.get("handoff_status") or "not_ready", 0) + 1
        by_job = {}
        for job in jobs:
            by_job[job.get("state") or "queued"] = by_job.get(job.get("state") or "queued", 0) + 1
        return {
            "versions": len(versions),
            "by_documentary_state": by_doc,
            "by_run_status": by_run,
            "by_handoff_status": by_hand,
            "by_job_state": by_job,
            "open_discrepancies": len(self.list_discrepancies(center_ids)) if reviewer else 0,
        }

    def list_users(self):
        rows = self._list("users", {}, [("login", ASCENDING)])
        for row in rows:
            row.pop("password_hash", None)
        return rows

    def insert_user(self, doc):
        self._insert("users", doc, "users")

    def get_user_by_login(self, login_normalized):
        return self._one("users", {"login_normalized": login_normalized})

    def get_user(self, user_id):
        return self._one("users", {"id": user_id})

    def insert_token(self, doc):
        self._insert("service_tokens", doc, "service_tokens")

    def get_token(self, token_id):
        return self._one("service_tokens", {"id": token_id})

    def revoke_token(self, token_id):
        self.db.service_tokens.update_one({"id": token_id}, {"$set": {"active": False}}, session=self.session)

    def list_stale_files(self, older_than):
        return self._list(
            "files",
            {"persistence_state": {"$ne": "ready"}, "created_at": {"$lt": older_than}},
            [("created_at", ASCENDING)],
        )

    def file_referenced(self, file_id) -> bool:
        if self._one("versions", {"file_id": file_id}):
            return True
        record = self._one("files", {"id": file_id})
        if not record or not record.get("extraction_id"):
            return False
        return self._one("extractions", {"id": record["extraction_id"]}) is not None

    def file_has_active_job(self, file_id) -> bool:
        job = self.db.jobs.find_one(
            {"file_id": file_id, "state": {"$in": ["queued", "running"]}},
            session=self.session,
        )
        return job is not None

    def delete_file_record(self, file_id):
        self.db.files.delete_one({"id": file_id, "persistence_state": {"$ne": "ready"}}, session=self.session)

    def topology(self) -> dict:
        hello = self.db.command("hello", session=self.session)
        return {
            "set_name": hello.get("setName"),
            "writable_primary": bool(hello.get("isWritablePrimary")),
        }

    def _insert(self, collection, doc, index):
        try:
            self.db[collection].insert_one(dict(doc), session=self.session)
        except DuplicateKeyError as exc:
            raise StoreConflict(index) from exc

    def _one(self, collection, query):
        return self._clean(self.db[collection].find_one(query, session=self.session))

    def _list(self, collection, query, sort, limit=0):
        cursor = self.db[collection].find(query, session=self.session)
        if sort:
            cursor = cursor.sort(sort)
        if limit:
            cursor = cursor.limit(limit)
        return [self._clean(doc) for doc in cursor]

    @staticmethod
    def _clean(doc):
        if not doc:
            return None
        copied = dict(doc)
        copied.pop("_id", None)
        copied.pop("password_hash", None) if False else None
        return copied


def _encode_cursor(moment: str, version_id: str) -> str:
    raw = json.dumps({"t": moment, "id": version_id}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(token: str) -> tuple[str, str]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.encode("ascii")))
        return payload["t"], payload["id"]
    except (KeyError, ValueError, json.JSONDecodeError):
        return "", ""


class MongoGateway:
    def __init__(self, client: MongoClient, db_name: str) -> None:
        self.client = client
        self.db_name = db_name
        self.store = MongoMetadataStore(client[db_name])

    def transaction(self, fn):
        last_error = None
        for _attempt in range(3):
            session = self.client.start_session()
            try:
                session.start_transaction()
                store = MongoMetadataStore(self.client[self.db_name], session=session)
                result = fn(store)
                session.commit_transaction()
                return result
            except Exception as exc:
                last_error = exc
                if session.in_transaction:
                    session.abort_transaction()
                labels = set(getattr(exc, "error_labels", []) or [])
                if "TransientTransactionError" not in labels or isinstance(exc, StoreConflict):
                    raise
            finally:
                session.end_session()
        raise last_error
