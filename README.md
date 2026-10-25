# Tower to AWX Migration Script: User Guide

*Scripts: `tower_awx_migrate.py` and `awx_set_creators.py` · Tower 3.8.x → AWX 24.6.x · Written October 25, 2026*

## 1. What it does

`tower_awx_migrate.py` copies a Tower 3.8 setup to AWX 24 through the REST API. It also carries over credential secrets, which the Tower API never returns, by decrypting them locally on the Tower VM.

It migrates:

- Organizations and teams (same names and descriptions)
- Users, if you pass `--create-users` (local users only; LDAP/SSO users are skipped)
- Org memberships, team memberships and org-level roles
- Custom credential types (and built-in types with `--all-credentials`)
- Credentials with their decrypted secret values
- Who has access to each credential (Admin, Use, Read for users and teams)
- Original creators and timestamps, as a second step on the AWX side (`awx_set_creators.py`)

It is a **dry run by default**, never deletes or overwrites anything on AWX, and is safe to re-run.

**Version note:** this guide matches the latest script (Appendix B). Older copies that wait 30 seconds with no output, or that have no `contacting source Tower ...` log line, or that call only `decrypt_field`, should be replaced.

## 2. How it works

1. Connects to Tower and AWX with API tokens and reads the source: orgs, teams, users, credential types, credentials.
2. Compares credential types with AWX by name. A matching type is reused. A missing custom type is queued for creation. A same-named type with different fields is reported and its credentials are skipped.
3. Decrypts secrets **before any write**. The script runs `sudo -u awx awx-manage shell` on the Tower VM and feeds it a short snippet through stdin. Inside Tower's own environment the snippet reads each credential and decrypts its secret fields (`get_input`, with `decrypt_field` as a fallback). The result returns to the script through a pipe as one JSON line. It is not written to disk or the log. A value that is empty or still starts with `$encrypted$` is treated as a failure and that credential is skipped.
4. Creates objects in this order: organizations, teams, users (optional), org and team memberships and roles, credential types, credentials, credential access.
5. Sends each credential to AWX over HTTPS as plain values. AWX encrypts them with its own key when it saves them, so the two systems do not need to share a key.
6. Writes `creator_map_<timestamp>.json` for the creator-restore step.

## 3. Prerequisites

- **Run it on the Tower VM.** Secret decryption needs local access to Tower's `awx-manage` and `SECRET_KEY`.
- **Python 3.6 or newer with `requests`.** If the system Python lacks it, use Tower's interpreter: `/var/lib/awx/venv/awx/bin/python3`.
- **`sudo -n -u awx awx-manage` must work without a password prompt**, or run the script as the `awx` user or root. If `awx-manage` is not at `/usr/bin/awx-manage`, set `TOWER_AWX_MANAGE` to its path.
- **Network access from the Tower VM to AWX**, for example `https://10.20.1.4:9443`. Use private addresses where you can.
- **Two API tokens:** one from Tower (admin or superuser, so it can see everything) and one from AWX (admin or superuser, scope Write). Create them in the web UI under Users, your user, Tokens.
- **A dummy credential or small test org** to try first.

## 4. Running it

**Step 1. Copy the scripts to the Tower VM.**

```
scp tower_awx_migrate.py awx_set_creators.py youruser@tower-vm:/tmp/migrate/
ssh youruser@tower-vm
cd /tmp/migrate && chmod 700 .
```

**Step 2. Check connectivity.** Each command should print `200`.

```
curl -k -m 10 -s -o /dev/null -w "%{http_code}\n" https://127.0.0.1/api/v2/ping/
curl -k -m 10 -s -o /dev/null -w "%{http_code}\n" https://10.20.1.4:9443/api/v2/ping/
```

From the Tower VM, point the source at `https://127.0.0.1`. Many cloud VMs cannot reach their own public IP, and the script then times out on its first call.

**Step 3. Set the tokens without writing them to shell history.** If you skip this, the script prompts for them.

```
read -rs TOWER_TOKEN; read -rs AWX_TOKEN; export TOWER_TOKEN AWX_TOKEN
```

**Step 4. Dry run.** Nothing is changed. Read every `WOULD CREATE` and `WOULD GRANT` line and compare it with Tower.

```
python3 tower_awx_migrate.py --src-url https://127.0.0.1 --dest-url https://10.20.1.4:9443 --insecure --create-users
```

For a first test, add `--org "SomeSmallOrg"`. Check that no LDAP users appear in `WOULD CREATE user` lines.

**Step 5. Execute.** Same command with `--execute` added.

**Step 6. Verify on AWX.** Check organizations, teams and their members, credential types, and the credentials with their access. Open a test credential and confirm a job can use it.

**Step 7. Restore creators (optional).** See section 7.

**Step 8. Clean up.** Delete both API tokens, delete `new_user_passwords.txt` once the passwords are distributed, and remove `/tmp/migrate`.

## 5. Options

| Option | Meaning |
| --- | --- |
| `--src-url` | Tower URL. From the Tower VM use `https://127.0.0.1`. |
| `--dest-url` | AWX URL, for example `https://10.20.1.4:9443`. |
| `--execute` | Apply changes. Without it, nothing is written. |
| `--insecure` | Skip TLS verification (self-signed certificate or no CA). The token and secrets are then sent to a server nobody has verified. |
| `--allow-http` | Allow plain `http://` URLs. Secrets then travel unencrypted. |
| `--create-users` | Create every missing local Tower user on AWX with a random password, saved to `new_user_passwords.txt`. LDAP/SSO users are never created. |
| `--all-credentials` | Also migrate credentials of built-in types such as Machine and Source Control. The default is custom types only. |
| `--org NAME` | Limit to one organization (repeatable). User-owned credentials are then excluded. |

Environment variables: `TOWER_TOKEN`, `AWX_TOKEN`, `TOWER_AWX_MANAGE`. If you have a CA certificate later, `REQUESTS_CA_BUNDLE=/path/ca.crt` lets you drop `--insecure`.

## 6. Output files

| File | Contents |
| --- | --- |
| `migration_<timestamp>.log` | Everything the script did. Created with 0600 permissions. It contains names and IDs but no secrets. |
| `creator_map_<timestamp>.json` | Original creator, modifier and timestamps for each org, team and credential created in that run (execute mode only). |
| `new_user_passwords.txt` | Random passwords for users created by `--create-users`. Distribute securely, then delete. |

## 7. Restoring original creators

The API cannot set `created_by`, so this runs inside AWX. Copy `creator_map_<timestamp>.json` and `awx_set_creators.py` to the AWX VM, then run the commands below. They assume AWX on Kubernetes in the `awx` namespace. The pod name changes when the pod restarts, so look it up with `sudo kubectl get pods -n awx | grep awx-task`.

```
sudo kubectl cp ~/creator_map_XXXX.json awx/<awx-task-pod>:/tmp/creator_map.json -c awx-task
sudo kubectl exec -i -n awx <awx-task-pod> -c awx-task -- env CREATOR_MAP=/tmp/creator_map.json awx-manage shell < ~/awx_set_creators.py
```

The output has one line per object, such as `Credential 12 my-cred updated`. Creators who do not exist on AWX are skipped, but the original timestamps are still restored. Do not run `awx_set_creators.py` directly with `python3`. It only works inside `awx-manage shell`.

## 8. Limitations

- **Tested only against a mock API.** It has not been run against a real Tower 3.8 or AWX 24.6. Run it on dummy credentials first.
- **Not migrated:** projects, inventories, hosts, job templates, workflows, schedules, notifications, labels, execution environments, instance groups, system settings, API tokens, and external secret-manager references (credential input sources).
- **Only objects inside the migrated organizations are copied.** Org-level roles are copied, but the objects they apply to are not there yet.
- **Additive only.** Existing orgs, teams and credentials on AWX are never changed or removed. If a secret changes on Tower after a credential was migrated, a re-run does not update it. Role grants are added to existing objects, but nothing is revoked.
- **Names are the match key.** Orgs, teams, users and credential types match by exact name or username. A renamed object counts as a different one.
- **Users:** passwords cannot be migrated. LDAP/SSO users are detected from the `ldap_dn` and `external_account` fields, and are skipped until they have logged in to AWX once. After that, re-run the script to attach their memberships. The superuser and system-auditor flags are copied for users the script creates. Check the dry run for `(SUPERUSER)`.
- **Credential types:** a same-named type on AWX with different field IDs or secret flags causes its credentials to be skipped. Built-in types must already exist on AWX under the same name.
- **User-owned credentials** (no organization) are created under the API token's user on AWX. The original owners still receive their access grants, but the token user keeps Admin too. With `--org`, these credentials are excluded.
- **Original creator and timestamps** need the separate AWX-side step, and only for objects created in that run.
- **No rollback.** If a run stops partway, what was created stays. Re-run to continue, or delete the objects manually. The log lists everything created.
- **Secrets are held in memory.** All selected credentials are decrypted up front and stay in the script's memory until it exits.

## 9. Security notes

- `--insecure` disables certificate checking. Use it only on a network you trust, and prefer a CA bundle once one exists.
- The script refuses plain `http://` unless you pass `--allow-http`.
- Secrets are never printed or logged. Error messages for credential requests show only which fields were rejected.
- Tokens are read from environment variables or hidden prompts, not from command-line arguments.
- Delete the API tokens and `new_user_passwords.txt` when you are done.

## 10. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `ConnectTimeout` on the source | The Tower VM cannot reach its own public IP. Use `--src-url https://127.0.0.1`. |
| `ConnectTimeout` on the destination | A firewall or network security group blocks the port. Test with the `curl` command in step 2. |
| `HTTP 401` | The token is wrong or expired, or was created on the other system. |
| `HTTP 403` | The token user lacks permission. Use an admin or superuser token with Write scope on AWX. |
| `cannot run awx-manage locally` | `sudo -n -u awx` is prompting for a password or `awx-manage` is not found. Set `TOWER_AWX_MANAGE`, or run as the `awx` user or root. |
| `ModuleNotFoundError: requests` | Use `/var/lib/awx/venv/awx/bin/python3` or install `requests` for your Python. |
| `SKIP credential ... ValueError` | Decryption failed for that credential. Test it by hand: `sudo -u awx awx-manage shell`, then `Credential.objects.get(pk=ID).get_input('field')`. |
| `SKIP credential ... required fields unavailable` | A required field is empty on Tower, or comes from an external secret source. |
| `type ... exists on AWX with different fields` | Rename or fix the type on AWX, then re-run. |
| `user X missing on AWX` | Re-run with `--create-users` or create the user manually. |
| `user X is LDAP/SSO` | The user must log in to AWX once. Re-run afterwards. |
| `grant failed` | A single role grant was rejected. The script continues, so check the log line. |

## 11. How Tower stores secrets, how the script decrypts them, and why that is safe

Tower keeps every credential secret encrypted in its database, and the script reads them only by running Tower's own decryption code on the Tower VM, with Tower's own key. This section describes the open-source AWX code that Tower 3.8 is built on, from general knowledge rather than from your server, so confirm the details with the checks in 11.5.

**11.1 How Tower stores secrets**

- **Where:** in the PostgreSQL table `main_credential`, in the JSON column `inputs`. Fields such as a username or URL are plain text. Fields the credential type marks as secret (password, token, private key) are encrypted.
- **Format:** an encrypted value is a string that starts with `$encrypted$`, then an algorithm tag, then base64 data. Recent releases use `$encrypted$AESCBC$...` (AES-256 in CBC mode). Older ones used `$encrypted$AES$...`.
- **Key:** each value gets its own key, built by hashing three things with SHA-512: Tower's `SECRET_KEY` (the file `/etc/tower/SECRET_KEY` on the Tower VM), the credential's database ID, and the field name. A value therefore decrypts only with the same key file, the same credential ID and the same field name.
- **What the API shows:** never the secret and never the ciphertext. It returns the literal text `$encrypted$`, and sending that text back in an update means keep what is stored.
- **Other secrets:** the same mechanism protects notification passwords, the LDAP bind password and similar settings.

**11.2 Why the encrypted data cannot simply be copied**

AWX has its own `SECRET_KEY`, and its credentials get different database IDs, so Tower's ciphertext would not decrypt there. The secret has to be decrypted on Tower, sent as a plain value over HTTPS, and encrypted again by AWX with its own key.

**11.3 How the script decrypts**

1. `local_secrets()` starts `sudo -n -u awx env GIT_PYTHON_REFRESH=quiet /usr/bin/awx-manage shell` (without `sudo` when already running as `awx`) and writes a short Python snippet to its stdin. The snippet carries only credential IDs and field definitions, never secrets.
2. `awx-manage shell` loads Tower's own Django environment, with the real `SECRET_KEY` and database access, as if Tower itself were running.
3. For each credential the snippet reads every field of its type. Non-secret fields are copied as stored. Secret fields go through `obj.get_input(field)`, Tower's supported accessor, which uses Tower's decrypt helper `decrypt_field` from `awx.main.utils.encryption`. If `get_input` raises an error, the snippet calls `decrypt_field(obj, field)` directly.
4. A result that is empty, or still starts with `$encrypted$`, raises an error. That credential is skipped and nothing is sent for it.
5. The snippet prints one line, `MIGRATE_JSON:{...}`, to stdout. The script reads it through a pipe, parses it, and keeps the values in memory.
6. The script sends each credential to AWX with `POST /api/v2/credentials/`, with the values in `inputs`, over HTTPS. AWX encrypts them with its own key.

The core of the snippet is:

```python
obj = Credential.objects.get(pk=12)
for f in obj.credential_type.inputs["fields"]:
    if f.get("secret"):
        value = obj.get_input(f["id"])   # Tower's supported accessor
        # fallback: decrypt_field(obj, f["id"])  (from awx.main.utils import decrypt_field)
```

The full snippet is the `CODE` string in Appendix B.

**11.4 Why this is reasonably safe, and where it is not**

| Safeguard | What it does |
| --- | --- |
| Decryption stays on the Tower VM | Tower's own code and key do the work. The key file never leaves the VM. Only the values go out, over HTTPS, to AWX. |
| No new access | Only someone with root or `sudo -u awx` on the Tower VM can run it, and that person can already read the key file and the database. |
| Not in command lines | The snippet arrives on stdin and the results return on stdout, so credential secrets do not appear in `ps` output, shell history or environment variables. |
| Not logged or written to disk | The log holds names and IDs, never values. Errors from credential requests list only the rejected field names. |
| No ciphertext is sent | A decrypt result that is empty, or still starts with `$encrypted$`, makes the script skip that credential. |
| Encrypted again on AWX | AWX encrypts each value with its own key when it saves it, and its API shows only `$encrypted$`. |
| Limited destinations | The script talks only to the two URLs you give, refuses paging links to other hosts, and refuses plain `http://` unless you pass `--allow-http`. |

A dry run also decrypts every selected credential, to catch failures before any write, but sends nothing to AWX.

The risks that remain:

- **`--insecure` removes certificate checking.** Someone on the network path could capture the AWX token and every secret. Use it only on a trusted private network, or set `REQUESTS_CA_BUNDLE` instead.
- **Plaintext sits in memory for the whole run.** All selected credentials are decrypted up front, so values could reach swap or a core dump. Run it on a locked-down host and let it exit when done.
- **The person running it can see every secret.** Review the `sudo` audit log afterwards.
- **`/etc/tower/SECRET_KEY` is the root of trust.** Anyone with that file and a copy of the database can decrypt everything, so protect both, including backups.
- **Manual checks print real secrets.** Terminal scrollback keeps them, so test with dummy credentials and clear the screen.
- **After the migration,** delete the API tokens, and consider rotating the most sensitive secrets, since they passed through a script and a network hop.

**11.5 Verify this on your own Tower**

On the Tower VM, open the shell and use a dummy credential:

```
sudo -u awx awx-manage shell
```

```python
from awx.main.models import Credential
c = Credential.objects.get(pk=12)
c.inputs                       # secret fields show $encrypted$AESCBC$... (ciphertext, not the secret)
c.get_input("password")        # prints the decrypted value; clear your screen afterwards
import inspect
from awx.main.utils import encryption
print(inspect.getsource(encryption))   # the exact algorithm and key derivation on your system
```

Replace `password` with a secret field name of that credential. The prefix on `c.inputs` confirms which algorithm your Tower uses, and the printed source shows exactly how the key is built.

## Appendix A: awx\_set\_creators.py

Run inside the AWX task pod through `awx-manage shell`, as described in section 7.

```python
# Restores original created_by / modified_by / created / modified on objects the migration created.
# Run ON the AWX host after the migration (the API cannot set these):
#   sudo -u awx env CREATOR_MAP=/tmp/creator_map_XXXX.json awx-manage shell < awx_set_creators.py
# (AWX on Kubernetes: copy the json into the awx-task pod and run the same command there.)
import json, os
from django.contrib.auth.models import User
from awx.main.models import Organization, Team, Credential
MODELS = {"Organization": Organization, "Team": Team, "Credential": Credential}
users = {u.username: u for u in User.objects.all()}
for e in json.load(open(os.environ["CREATOR_MAP"])):
    upd = {}
    if e.get("created_by") in users:
        upd["created_by"] = users[e["created_by"]]
    if e.get("modified_by") in users:
        upd["modified_by"] = users[e["modified_by"]]
    if e.get("created"):
        upd["created"] = e["created"]
    if e.get("modified"):
        upd["modified"] = e["modified"]
    qs = MODELS[e["model"]].objects.filter(pk=e["id"])
    n = qs.update(**upd) if upd else 0
    print(e["model"], e["id"], e.get("name"), "updated" if n else "skipped")
```

## Appendix B: tower\_awx\_migrate.py

```python
#!/usr/bin/env python3
"""Tower 3.8 -> AWX 24: orgs, teams, users, org/team membership and roles, custom
credential types, credentials (with secrets) and who can access each credential.
Dry run unless --execute. After a real run, awx_set_creators.py (run on the AWX
host) restores original created_by / created timestamps from creator_map_*.json.

Run it ON the Tower host: secrets are decrypted locally via awx-manage, because
the Tower API never returns them. Tokens: env TOWER_TOKEN / AWX_TOKEN, else prompted.
Never overwrites or deletes anything on AWX; safe to re-run.
"""
import argparse, getpass, json, logging, os, re, secrets, subprocess, sys, time
from urllib.parse import urlparse
import requests

NEW = -1  # id standing in for "would be created" during a dry run
log = logging.getLogger("migrate")
CREATORS = []  # created objects, written to creator_map_*.json for awx_set_creators.py


class API(object):
    def __init__(self, base, token, verify):
        self.base, self.verify = base, verify
        self.s = requests.Session()
        self.s.headers["Authorization"] = "Bearer " + token

    def call(self, method, path, **kw):
        url = path if path.startswith("http") else self.base + path
        if urlparse(url).netloc != urlparse(self.base).netloc:
            raise RuntimeError("refusing URL outside configured host")
        try:
            r = self.s.request(method, url, verify=self.verify, timeout=(10, 60), **kw)
        except requests.RequestException as e:
            hint = " (TLS problem? try --insecure)" if "SSL" in type(e).__name__ else ""
            raise RuntimeError("%s %s%s failed: %s%s" % (method, self.base, path, type(e).__name__, hint))
        if r.status_code >= 400:
            try:  # never echo credential bodies; show only which fields were rejected
                body = r.json()
                detail = r.text[:300] if "/credentials/" not in path else ",".join(body)
            except Exception:
                detail = ""
            raise RuntimeError("%s %s -> HTTP %s %s" % (method, path, r.status_code, detail))
        return r.json() if r.content else None

    def all(self, path):
        out, params = [], {"page_size": 200}
        while path:
            d = self.call("GET", path, params=params)
            params = None
            out += d["results"]
            path = d.get("next")
        return out


def is_custom(t):
    return t.get("managed") is False or t.get("managed_by_tower") is False


def fields(t):
    return (t.get("inputs") or {}).get("fields") or []


CODE = r'''
import json
from awx.main.models import Credential
from awx.main.utils import decrypt_field
req = json.loads(__REQ__)
out = {}
for cid, flds in req.items():
    try:
        obj = Credential.objects.get(pk=int(cid))
        vals = {}
        for f in flds:
            v = obj.inputs.get(f["id"])
            if v in (None, ""):
                continue
            if f.get("secret"):
                try:
                    v = obj.get_input(f["id"])
                except Exception:
                    v = decrypt_field(obj, f["id"])
                if not v or str(v).startswith("$encrypted$"):
                    raise ValueError("decrypt failed")
            vals[f["id"]] = v
        out[cid] = vals
    except Exception as e:
        out[cid] = {"__error__": type(e).__name__}
print("MIGRATE_JSON:" + json.dumps(out))
'''


def local_secrets(req):
    """{cred_id: [field defs]} -> {cred_id: {field: plaintext}} via local awx-manage."""
    mng = os.environ.get("TOWER_AWX_MANAGE") or "/usr/bin/awx-manage"
    sudo = [] if getpass.getuser() == "awx" else ["sudo", "-n", "-u", "awx"]
    cmd = sudo + ["env", "GIT_PYTHON_REFRESH=quiet", mng, "shell"]
    try:
        p = subprocess.run(cmd, input=CODE.replace("__REQ__", repr(json.dumps(req))),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError("cannot run awx-manage locally: %s" % type(e).__name__)
    if p.returncode:
        raise RuntimeError("awx-manage failed: " + " | ".join(p.stderr.splitlines()[-5:]))
    lines = [l[13:] for l in p.stdout.splitlines() if l.startswith("MIGRATE_JSON:")]
    if len(lines) != 1:
        raise RuntimeError("unexpected awx-manage output")
    return json.loads(lines[0])


def base_url(u, allow_http):
    u = re.sub(r"/api/v2$", "", u.strip().rstrip("/"))
    if not (u.startswith("https://") or (allow_http and u.startswith("http://"))):
        raise RuntimeError("URL must be https:// (or pass --allow-http): %s" % u)
    return u


def migrate(a, st, dt):
    go = a.execute
    if a.insecure:
        requests.packages.urllib3.disable_warnings()
        log.warning("TLS verification DISABLED: token and secrets are sent unverified")
    src = API(base_url(a.src_url, a.allow_http), st, not a.insecure)
    dst = API(base_url(a.dest_url, a.allow_http), dt, not a.insecure)
    for name, api in (("source Tower", src), ("destination AWX", dst)):
        log.info("contacting %s at %s ...", name, api.base)
        log.info("%s version %s", name, api.call("GET", "/api/v2/ping/").get("version"))
    me = dst.call("GET", "/api/v2/me/")["results"][0]["id"]
    log.info("MODE: %s", "EXECUTE" if go else "DRY RUN (nothing will change; add --execute)")

    def ensure(path, existing, match, payload, what, model=None, obj=None):
        for e in existing:
            if all(e.get(k) == v for k, v in match.items()):
                return e["id"], False
        log.info("%s %s", "CREATE" if go else "WOULD CREATE", what)
        if not go:
            return NEW, True
        r = dst.call("POST", path, json=payload)
        existing.append(r)
        if model:
            sf = obj.get("summary_fields") or {}
            CREATORS.append({"model": model, "id": r["id"], "name": obj.get("name"),
                             "created_by": (sf.get("created_by") or {}).get("username"),
                             "modified_by": (sf.get("modified_by") or {}).get("username"),
                             "created": obj.get("created"), "modified": obj.get("modified")})
        return r["id"], True

    # ---- read source ----
    src_orgs = [o for o in src.all("/api/v2/organizations/") if not a.org or o["name"] in a.org]
    org_ids = set(o["id"] for o in src_orgs)
    src_teams = [t for t in src.all("/api/v2/teams/") if t["organization"] in org_ids]
    src_types = dict((t["id"], t) for t in src.all("/api/v2/credential_types/"))
    creds = [c for c in src.all("/api/v2/credentials/")
             if (a.all_credentials or is_custom(src_types[c["credential_type"]]))
             and (c["organization"] in org_ids or (c["organization"] is None and not a.org))]
    log.info("source: %d orgs, %d teams, %d credentials selected", len(src_orgs), len(src_teams), len(creds))

    # ---- classify credential types (read-only) ----
    dst_types = dst.all("/api/v2/credential_types/")
    type_map, new_types = {}, {}
    for tid in set(c["credential_type"] for c in creds):
        t = src_types[tid]
        same = [d for d in dst_types if d["name"] == t["name"] and is_custom(d) == is_custom(t)]
        sig = lambda x: sorted((f["id"], bool(f.get("secret"))) for f in fields(x))
        if same:
            if sig(t) != sig(same[0]):
                log.error("type %r exists on AWX with different fields; skipping its credentials", t["name"])
                continue
            if json.dumps(t.get("injectors"), sort_keys=True) != json.dumps(same[0].get("injectors"), sort_keys=True):
                log.warning("type %r: injectors differ from AWX copy (kept AWX's)", t["name"])
            type_map[tid] = same[0]["id"]
        elif is_custom(t):
            new_types[tid] = t
        else:
            log.error("built-in type %r not found on AWX; skipping its credentials", t["name"])
    usable = [c for c in creds if c["credential_type"] in type_map or c["credential_type"] in new_types]

    # ---- decrypt secrets locally BEFORE any write ----
    vals = local_secrets(dict((str(c["id"]), fields(src_types[c["credential_type"]])) for c in usable)) if usable else {}
    ready = {}
    for c in usable:
        spec = src_types[c["credential_type"]].get("inputs") or {}
        v = vals.get(str(c["id"]), {})
        allowed = set(f["id"] for f in fields(src_types[c["credential_type"]]))
        inputs = dict((k, x) for k, x in v.items() if k in allowed and x is not None)
        missing = [r for r in spec.get("required", []) if inputs.get(r) in (None, "")]
        if "__error__" in v or missing:
            log.error("SKIP credential %s (id %s): %s", c["name"], c["id"],
                      v.get("__error__") or "required fields unavailable: " + ", ".join(missing))
            continue
        ready[c["id"]] = inputs

    # ---- users ----
    src_users = dict((u["id"], u) for u in src.all("/api/v2/users/"))
    dst_users = dict((u["username"], u["id"]) for u in dst.all("/api/v2/users/"))
    warned = set()

    def user_id(sid):
        u = src_users.get(sid)
        if not u:
            return None
        name = u["username"]
        if name in dst_users:
            return dst_users[name]
        if name in warned:
            return None
        warned.add(name)
        if u.get("ldap_dn") or u.get("external_account"):
            log.warning("user %s is LDAP/SSO: not created (appears on first login); access skipped", name)
            return None
        if not a.create_users:
            log.warning("user %s missing on AWX (use --create-users); access skipped", name)
            return None
        log.info("%s user %s%s", "CREATE" if go else "WOULD CREATE", name,
                 " (SUPERUSER)" if u.get("is_superuser") else "")
        if not go:
            dst_users[name] = NEW
            return NEW
        pw = secrets.token_urlsafe(16)
        payload = dict((k, u.get(k) or "") for k in ("username", "first_name", "last_name", "email"))
        payload.update(password=pw, is_superuser=bool(u.get("is_superuser")),
                       is_system_auditor=bool(u.get("is_system_auditor")))
        dst_users[name] = dst.call("POST", "/api/v2/users/", json=payload)["id"]
        with os.fdopen(os.open("new_user_passwords.txt", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as f:
            f.write("%s %s\n" % (name, pw))
        return dst_users[name]

    def replay(kind, sid, did, label):
        """Copy every role holder (users + teams) of a source org/team/credential to its AWX twin.
        Org Member/Admin and team Member roles ARE the memberships. Additive only."""
        apply = go and did != NEW
        droles = dict((r["name"], r["id"]) for r in dst.all("/api/v2/%s/%s/object_roles/" % (kind, did))) if apply else None
        for r in src.all("/api/v2/%s/%s/object_roles/" % (kind, sid)):
            if droles is not None and r["name"] not in droles:
                continue
            holders = [("users", u["username"], user_id(u["id"])) for u in src.all("/api/v2/roles/%s/users/" % r["id"])]
            holders += [("teams", t["name"], team_map.get(t["id"])) for t in src.all("/api/v2/roles/%s/teams/" % r["id"])]
            for hk, name, target in holders:
                if target is None:
                    if hk == "teams":
                        log.warning("team %s (role %s on %s) not migrated; skipped", name, r["name"], label)
                    continue
                log.info("%s %s -> %s %s on %s", "GRANT" if apply else "WOULD GRANT", r["name"], hk[:-1], name, label)
                if apply and target != NEW:
                    try:
                        dst.call("POST", "/api/v2/%s/%s/roles/" % (hk, target), json={"id": droles[r["name"]]})
                    except RuntimeError as e:
                        log.warning("grant failed: %s", e)

    # ---- orgs, teams, memberships ----
    dst_orgs = dst.all("/api/v2/organizations/")
    org_map = {}
    for o in src_orgs:
        org_map[o["id"]], _ = ensure("/api/v2/organizations/", dst_orgs, {"name": o["name"]},
                                     {"name": o["name"], "description": o.get("description") or ""},
                                     "organization %s" % o["name"], "Organization", o)
    dst_teams = dst.all("/api/v2/teams/")
    team_map = {}
    for t in src_teams:
        d = org_map[t["organization"]]
        team_map[t["id"]], _ = ensure("/api/v2/teams/", dst_teams, {"name": t["name"], "organization": d},
                                      {"name": t["name"], "description": t.get("description") or "", "organization": d},
                                      "team %s" % t["name"], "Team", t)
    if a.create_users:
        for sid in src_users:
            user_id(sid)
    for o in src_orgs:
        replay("organizations", o["id"], org_map[o["id"]], "org " + o["name"])
    for t in src_teams:
        replay("teams", t["id"], team_map[t["id"]], "team " + t["name"])

    # ---- credential types ----
    for tid, t in new_types.items():
        log.info("%s credential type %s", "CREATE" if go else "WOULD CREATE", t["name"])
        payload = {"name": t["name"], "description": t.get("description") or "", "kind": t.get("kind") or "cloud",
                   "inputs": t.get("inputs") or {}, "injectors": t.get("injectors") or {}}
        type_map[tid] = dst.call("POST", "/api/v2/credential_types/", json=payload)["id"] if go else NEW

    # ---- credentials + access ----
    dst_creds = dst.all("/api/v2/credentials/")
    made = existed = failed = 0
    for c in usable:
        if c["id"] not in ready:
            failed += 1
            continue
        try:
            torg = org_map[c["organization"]] if c["organization"] else None
            dtype = type_map[c["credential_type"]]
            payload = {"name": c["name"], "description": c.get("description") or "",
                       "credential_type": dtype, "inputs": ready[c["id"]]}
            payload.update({"organization": torg} if torg is not None else {"user": me})
            did, created = ensure("/api/v2/credentials/", dst_creds,
                                  {"name": c["name"], "credential_type": dtype, "organization": torg},
                                  payload, "credential %s (id %s)" % (c["name"], c["id"]), "Credential", c)
            if created:
                made += 1
            else:
                existed += 1
                log.info("EXISTS credential %s: not overwritten, access re-applied", c["name"])
            replay("credentials", c["id"], did, c["name"])
        except RuntimeError as e:
            failed += 1
            log.error("FAILED credential %s (id %s): %s", c["name"], c["id"], e)
    log.info("SUMMARY: credentials %s=%d existing=%d failed/skipped=%d",
             "created" if go else "to_create", made, existed, failed)
    return 0 if not failed else 3


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src-url", required=True, help="Tower URL, e.g. https://tower.example.com")
    ap.add_argument("--dest-url", required=True, help="AWX URL")
    ap.add_argument("--execute", action="store_true", help="apply changes (default: dry run)")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed / no CA)")
    ap.add_argument("--allow-http", action="store_true", help="allow plain http:// (secrets travel unencrypted!)")
    ap.add_argument("--all-credentials", action="store_true", help="also migrate credentials of built-in types")
    ap.add_argument("--create-users", action="store_true", help="create ALL missing local Tower users on AWX (random passwords -> new_user_passwords.txt); LDAP/SSO users are never created")
    ap.add_argument("--org", action="append", help="limit to this org name (repeatable); default: all orgs + user-owned credentials")
    a = ap.parse_args()
    st = os.environ.get("TOWER_TOKEN") or getpass.getpass("Tower token: ")
    dt = os.environ.get("AWX_TOKEN") or getpass.getpass("AWX token: ")
    path = "migration_%s.log" % time.strftime("%Y%m%dT%H%M%S")
    os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(path)])
    try:
        return migrate(a, st, dt)
    except RuntimeError as e:
        log.error("STOPPED: %s", e)
        return 2
    finally:
        if CREATORS:
            cm = "creator_map_%s.json" % time.strftime("%Y%m%dT%H%M%S")
            with os.fdopen(os.open(cm, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                json.dump(CREATORS, f, indent=1)
            log.info("wrote %s (%d objects) - run awx_set_creators.py on the AWX host to restore creators", cm, len(CREATORS))


if __name__ == "__main__":
    sys.exit(main())
```
