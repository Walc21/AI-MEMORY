"""Official Drive v3 adapter; the session plugin uses the separate MCP bridge."""

import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from Transformer_Core.Hot_Hub.hub import HubError, _open_regular
from Transformer_Core.semantic.model import SemanticError
from BN1_1.Pacote.cache import CacheError
from Transformer_Core.structural.model import StructuralError
from .model import IOError, account_fingerprint, export_spec, remote_id

SCOPES = ["https://www.googleapis.com/auth/drive"]
FIELDS = "id,name,mimeType,parents,size,version,modifiedTime,md5Checksum,sha256Checksum,webViewLink,trashed,appProperties,ownedByMe,shared"


class _GoogleRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or not (parsed.hostname == "www.googleapis.com" or (parsed.hostname or "").endswith(".googleusercontent.com")):
            raise IOError("Redirecionamento do Drive fora dos hosts oficiais bloqueado.")
        following = super().redirect_request(req, fp, code, msg, headers, newurl)
        if following and parsed.hostname != urllib.parse.urlsplit(req.full_url).hostname:
            following.remove_header("Authorization")
        return following


class DriveError(IOError):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason
        super().__init__(f"Google Drive HTTP {status}: {reason}")


def authorize(channels, client_secrets, token_file=None, port=0, open_browser=True):
    from google_auth_oauthlib.flow import InstalledAppFlow
    with channels.locked(write=True):
        target = Path(token_file) if token_file else channels.root / "credentials/google-token.json"
        from BN1_2.buffer import ensure_directories
        ensure_directories(target.parent, target.parent)
        if target.exists() or target.is_symlink():
            raise IOError("Arquivo de credenciais já existe; use-o ou selecione outro destino.")
        with _open_regular(Path(client_secrets)) as stream:
            client = json.load(stream)
        if "installed" not in client:
            raise IOError("OAuth requer credenciais de cliente Desktop do Google Cloud.")
        flow = InstalledAppFlow.from_client_config(client, scopes=SCOPES)
        credentials = flow.run_local_server(host="127.0.0.1", port=port, open_browser=open_browser,
                                           access_type="offline", prompt="consent", timeout_seconds=300)
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(credentials.to_json())
            stream.flush()
            os.fsync(stream.fileno())
        return {"authenticated": True, "credential_file": str(target), "scopes": SCOPES}


class DriveAPI:
    def __init__(self, credentials, timeout=30):
        self.credentials, self.timeout = credentials, timeout

    @classmethod
    def from_channels(cls, channels, token_file=None):
        from google.oauth2.credentials import Credentials
        target = Path(token_file or os.environ.get("MIMIR_GOOGLE_TOKEN_FILE", channels.root / "credentials/google-token.json"))
        if not target.exists():
            raise IOError("Credencial OAuth da API ausente. A credencial protegida do plugin não é exportável; use a ponte MCP ou drive auth.")
        with _open_regular(target) as stream:
            if os.fstat(stream.fileno()).st_mode & 0o077:
                raise IOError("Credencial OAuth deve ser privada (chmod 600).")
            info = json.load(stream)
        credentials = Credentials.from_authorized_user_info(info, scopes=SCOPES)
        return cls(credentials)

    def request(self, path, method="GET", body=None, mime="application/json", maximum=2 * 1024 * 1024):
        from google.auth.exceptions import GoogleAuthError, TransportError
        from google.auth.transport.requests import Request
        if not path.startswith(("/drive/v3/", "/upload/drive/v3/")):
            raise IOError("Rota externa fora da API oficial do Drive.")
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        headers = {"Accept": "application/json", "Content-Type": mime}
        url = "https://www.googleapis.com" + path
        try:
            self.credentials.before_request(Request(), method, url, headers)
        except TransportError:
            raise IOError("Rede indisponível durante renovação OAuth; tente novamente na próxima sincronização.") from None
        except GoogleAuthError:
            raise IOError("Não foi possível renovar a credencial OAuth; autentique novamente com drive auth em um novo arquivo de credenciais.") from None
        # GET is retryable. Writes use preallocated IDs and explicit reconciliation.
        for attempt in range(4 if method == "GET" else 1):
            try:
                with urllib.request.build_opener(_GoogleRedirect()).open(urllib.request.Request(url, data=data, method=method, headers=headers), timeout=self.timeout) as response:
                    value = response.read(maximum + 1)
                if len(value) > maximum:
                    raise IOError("Resposta do Drive excede o limite; nenhum conteúdo truncado foi aceito.")
                return value
            except urllib.error.HTTPError as exc:
                try:
                    detail = json.loads(exc.read(16384))["error"]
                    errors = detail.get("errors") or []
                    reason = errors[0].get("reason") if errors else None
                    reason = reason or detail.get("status", "request_failed")
                    if not isinstance(reason, str):
                        reason = "request_failed"
                except (ValueError, KeyError, TypeError, AttributeError, IndexError):
                    reason = "request_failed"
                finally:
                    exc.close()
                retryable = exc.code in {429, 500, 502, 503, 504} or reason in {"rateLimitExceeded", "userRateLimitExceeded"}
                if method == "GET" and retryable and attempt < 3:
                    time.sleep(min(8, .5 * 2 ** attempt))
                    continue
                if reason in {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}:
                    reason = "ACCESS_TOKEN_SCOPE_INSUFFICIENT: reconecte/autentique com leitura e escrita"
                raise DriveError(exc.code, reason) from None
            except (OSError, TimeoutError) as exc:
                if method == "GET" and attempt < 3:
                    time.sleep(min(8, .5 * 2 ** attempt))
                    continue
                raise IOError("Rede do Drive indisponível; operação permanece pendente para reconciliação.") from exc

    def json(self, path, **options):
        try:
            return json.loads(self.request(path, **options))
        except (ValueError, UnicodeError) as exc:
            raise IOError("Drive retornou JSON inválido.") from exc

    def metadata(self, identity):
        remote_id(identity)
        return self.json("/drive/v3/files/" + identity + "?" + urllib.parse.urlencode({"fields": FIELDS, "supportsAllDrives": "true"}))

    def profile(self):
        value = self.json("/drive/v3/about?fields=user(emailAddress,permissionId,displayName)")
        return value["user"]

    def _file_pages(self, options):
        options = dict(options)
        seen_tokens = set()
        while True:
            page = self.json("/drive/v3/files?" + urllib.parse.urlencode(options))
            if not isinstance(page, dict) or not isinstance(page.get("files", []), list) or any(not isinstance(row, dict) for row in page.get("files", [])):
                raise IOError("Drive retornou uma página de arquivos inválida.")
            yield page
            token = page.get("nextPageToken")
            if not token:
                break
            if not isinstance(token, str) or token in seen_tokens:
                raise IOError("Drive retornou paginação inválida ou repetida.")
            seen_tokens.add(token)
            options["pageToken"] = token

    def files(self, folder, max_files=10000):
        remote_id(folder)
        count = 0
        options = {"q": f"'{folder}' in parents and trashed = false", "fields": f"nextPageToken,files({FIELDS})",
                   "pageSize": 1000, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        for page in self._file_pages(options):
            for row in page.get("files", []):
                count += 1
                if count > max_files:
                    raise IOError("Pasta excede max_sync_files; aumente o limite explicitamente.")
                yield row

    def folder(self, name, parent="root"):
        if parent != "root":
            remote_id(parent)
        if name not in {"AI MEMORY - Mimir", "Entrada", "Saída"}:
            raise IOError("Bootstrap aceita somente as pastas do Mimir.")
        query = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and '{parent}' in parents and trashed = false"
        matches = []
        for page in self._file_pages({"q": query, "fields": f"nextPageToken,files({FIELDS})", "pageSize": 100}):
            matches.extend(page.get("files", []))
            if len(matches) > 1:
                raise IOError("Há pastas homônimas; vincule IDs explícitos pela ponte.")
        if matches:
            existing = matches[0]
            if existing.get("ownedByMe") is not True or existing.get("shared") is not False:
                raise IOError("Bootstrap requer pasta privada pertencente à conta atual.")
            return existing
        result = self.json("/drive/v3/files?" + urllib.parse.urlencode({"fields": FIELDS}), method="POST",
            body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent]})
        return self.metadata(result["id"])

    def download(self, metadata, maximum):
        remote_id(metadata["id"])
        export_mime, _ = export_spec(metadata)
        if export_mime:
            path = "/drive/v3/files/" + metadata["id"] + "/export?" + urllib.parse.urlencode({"mimeType": export_mime})
        else:
            path = "/drive/v3/files/" + metadata["id"] + "?alt=media&supportsAllDrives=true"
        return self.request(path, maximum=maximum)

    def reserve(self):
        return self.json("/drive/v3/files/generateIds?count=1&space=drive&type=files")["ids"][0]

    def upload(self, job, config, path):
        identity = remote_id(job["remote_reserved_id"])
        try:
            existing = self.metadata(identity)
            if existing.get("appProperties", {}).get("mimir_output_id") != job["id"]:
                raise IOError("ID remoto reservado já pertence a outro objeto.")
            return existing
        except DriveError as exc:
            if exc.status != 404:
                raise
        with _open_regular(path) as stream:
            data = stream.read(job["size"] + 1)
        if len(data) != job["size"] or hashlib.sha256(data).hexdigest() != job["sha256"]:
            raise IOError("Payload mudou antes do upload.")
        metadata = {"id": identity, "name": job["file_name"], "mimeType": "application/json",
                    "parents": [config["output_folder_id"]], "appProperties": {"mimir_output_id": job["id"]}}
        boundary = "mimir_" + uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode() + json.dumps(metadata).encode() +
                f"\r\n--{boundary}\r\nContent-Type: application/json\r\n\r\n".encode() + data + f"\r\n--{boundary}--\r\n".encode())
        result = self.json("/upload/drive/v3/files?" + urllib.parse.urlencode({"uploadType": "multipart", "fields": FIELDS, "supportsAllDrives": "true"}),
                           method="POST", body=body, mime="multipart/related; boundary=" + boundary,
                           maximum=2 * 1024 * 1024)
        return self.metadata(result["id"])


class DriveSync:
    def __init__(self, channels, api):
        self.channels, self.api = channels, api

    def identity(self, config):
        profile = self.api.profile()
        if account_fingerprint(profile["emailAddress"]) != config["account_fingerprint"]:
            raise IOError("Conta da API difere da conta autenticada vinculada ao namespace.")
        for key in ("root_folder_id", "input_folder_id", "output_folder_id"):
            metadata = self.api.metadata(config[key])
            if metadata.get("trashed") or metadata.get("mimeType") != "application/vnd.google-apps.folder":
                raise IOError("Pasta configurada foi removida ou alterada.")
            if metadata.get("shared") or metadata.get("ownedByMe") is False:
                raise IOError("Pasta configurada deixou de ser privada da conta vinculada.")
            if key != "root_folder_id" and config["root_folder_id"] not in metadata.get("parents", []):
                raise IOError("Entrada/saída saiu da raiz autorizada.")
        return profile

    def bootstrap(self):
        with self.channels.locked(write=True):
            if self.channels.config_file.exists():
                config = self.channels.config()
                self.identity(config)
                return config
            profile = self.api.profile()
            root = self.api.folder("AI MEMORY - Mimir")
            incoming = self.api.folder("Entrada", root["id"])
            outgoing = self.api.folder("Saída", root["id"])
        return self.channels.configure(root, incoming, outgoing, profile["emailAddress"], "api")

    def sync(self, **settings):
        with self.channels.locked():
            config = self.channels.config()
        self.identity(config)
        accepted, skipped, failures = [], [], []
        try:
            for item in self.api.files(config["input_folder_id"], self.channels.limits.max_sync_files):
                if item["mimeType"] == "application/vnd.google-apps.folder":
                    skipped.append({"file_id": item["id"], "reason": "direct_children_only"})
                    continue
                path = None
                try:
                    before = self.api.metadata(item["id"])
                    self.channels._input_metadata(before)
                    if not settings.get("force") and self.channels.processed(before):
                        skipped.append({"file_id": item["id"], "reason": "unchanged_revision"})
                        continue
                    data = self.api.download(before, self.channels.limits.max_file_bytes)
                    after = self.api.metadata(item["id"])
                    path = self.channels.stage(data)
                    accepted.append(self.channels.ingest_file(path, before, after, **settings))
                except (SemanticError, StructuralError, CacheError, HubError, OSError, ValueError) as exc:
                    failures.append({"file_id": item["id"], "error": str(exc) if isinstance(exc, IOError) else type(exc).__name__})
                finally:
                    if path is not None:
                        path.unlink(missing_ok=True)
        except (SemanticError, OSError, ValueError) as exc:
            # A partial or unavailable inbox must not strand previously queued outputs.
            failures.append({"input_folder_id": config["input_folder_id"],
                             "error": str(exc) if isinstance(exc, IOError) else type(exc).__name__})
        delivered = []
        for job in self.channels.pending(limit=10000):
            try:
                if job["remote_reserved_id"] is None:
                    job = {**self.channels.reserve(job["id"], self.api.reserve()), "local_path": job["local_path"]}
                remote = self.api.upload(job, config, Path(job["local_path"]))
                delivered.append(self.channels.acknowledge(job["id"], remote))
            except (SemanticError, OSError, ValueError) as exc:
                failures.append({"output_id": job["id"], "error": str(exc) if isinstance(exc, IOError) else type(exc).__name__})
        return {"schema": "mimir.drive-sync.v1", "backend": "api", "accepted": accepted,
                "delivered": delivered, "skipped": skipped, "failures": failures, "complete": not failures}
