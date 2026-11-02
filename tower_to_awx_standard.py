#!/usr/bin/env python3
"""Ansible Tower 3.8 -> AWX 24 migration tool.  Python 3.6+, standard library only.

Run it ON the Tower VM: credential secrets are decrypted locally with
"sudo -u awx awx-manage", because the Tower API never returns them.

  python3 tower_awx_migrate_v3.py            guided menu (easiest, recommended)
  python3 tower_awx_migrate_v3.py --help     every option, with examples

Nothing changes unless you choose "Migrate" (--execute). Nothing is ever
overwritten or deleted on AWX, so a run can be repeated safely. No files are
written unless you ask for them.
"""
import argparse, getpass, hashlib, hmac, json, os, secrets, ssl, subprocess, sys, time, traceback
import urllib.error, urllib.request
from urllib.parse import urlparse

NEW = -1  # stand-in id for "would be created" during a dry run
TIMEOUT = 45
LOG = None


class Stop(Exception):
    """The user chose to quit."""


class Fail(Exception):
    """An expected problem; the message is shown as is."""


# ----------------------------------------------------------------------------- console helpers
def say(msg=""):
    print(msg)
    if LOG:
        LOG.write(msg + "\n")
        LOG.flush()


def show(title, obj):  # stdout only: may contain plain-text secrets, never logged
    print("\n--- %s ---\n%s" % (title, json.dumps(obj, indent=2, sort_keys=True)))


def private_file(path, mode="w"):
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == "a" else os.O_TRUNC)
    return os.fdopen(os.open(path, flags, 0o600), mode)


def ask(prompt, default="", secret=False):
    try:
        if secret:
            v = getpass.getpass(prompt + ": ")
        else:
            v = input(prompt + (" [%s]" % default if default else "") + ": ")
    except EOFError:
        raise Stop()
    return v.strip() or default


def yesno(question, default=False):
    v = ask("%s [%s]" % (question, "Y/n" if default else "y/N")).lower()
    return default if not v else v[0] == "y"


def menu(title, options):
    print("\n" + title)
    for i, o in enumerate(options, 1):
        print("  %d) %s" % (i, o))
    while True:
        v = ask("Choose 1-%d" % len(options))
        if v.isdigit() and 1 <= int(v) <= len(options):
            return int(v) - 1
        print("  Please type one of the numbers above.")


def multi(title, labels):
    """Numbered list; type e.g. 1,3,5-7 or 'all'. Returns the chosen labels (empty list = cancelled)."""
    print("\n" + title)
    for i, l in enumerate(labels, 1):
        print("  %d) %s" % (i, l))
    while True:
        v = ask("Numbers (e.g. 1,3,5-7), 'all', or just ENTER to cancel").lower().replace(" ", "")
        if not v:
            return []
        if v == "all":
            return list(labels)
        try:
            idx = set()
            for part in v.split(","):
                lo, _, hi = part.partition("-")
                idx.update(range(int(lo), int(hi or lo) + 1))
            if idx and all(1 <= i <= len(labels) for i in idx):
                return [labels[i - 1] for i in sorted(idx)]
        except ValueError:
            pass
        print("  Could not read that. Example: 1,3,5-7")


# ----------------------------------------------------------------------------- HTTP (urllib)
class API(object):
    def __init__(self, name, base, token, insecure):
        self.name, self.base, self.token = name, base.rstrip("/"), token
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                  urllib.request.HTTPSHandler(context=ctx))

    def call(self, method, path, body=None, timeout=TIMEOUT):
        url = path if path.startswith("http") else self.base + path
        if urlparse(url).netloc != urlparse(self.base).netloc:
            raise Fail("refusing to follow a link to another host")
        req = urllib.request.Request(
            url, method=method, data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/json",
                     "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise Fail(self.explain(e, path))
        except (urllib.error.URLError, OSError) as e:
            why = getattr(e, "reason", e)
            hint = " - certificate problem? answer yes to 'skip TLS checks' / use --insecure" \
                if isinstance(why, (ssl.SSLError, ssl.CertificateError)) else ""
            raise Fail("cannot reach %s at %s (%s)%s" % (self.name, self.base, why, hint))
        try:
            return json.loads(raw.decode()) if raw else None
        except ValueError:
            raise Fail("%s sent something that is not JSON - is the URL / port right?" % self.name)

    def explain(self, e, path):
        if e.code == 401:
            return "%s rejected the token (HTTP 401) - wrong token, or a token from the other server" % self.name
        if e.code == 403:
            return "%s denied permission (HTTP 403) - use an admin/superuser token" % self.name
        if e.code == 404:
            return "%s has no such item (HTTP 404: %s)" % (self.name, path)
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            body = None
        detail = ""
        if isinstance(body, dict):
            if "/credentials/" in path:  # never echo credential values, only which fields were refused
                detail = "fields: " + ", ".join(body)
            else:
                detail = "; ".join("%s: %s" % (k, ", ".join(str(x) for x in v) if isinstance(v, list) else v)
                                   for k, v in body.items())
        return "%s refused it (HTTP %s)%s" % (self.name, e.code, ": " + detail[:300] if detail else "")

    def all(self, path):
        out = []
        path += ("&" if "?" in path else "?") + "page_size=200"
        while path:
            d = self.call("GET", path)
            out += d.get("results", [])
            path = d.get("next")
        return out


# ----------------------------------------------------------------------------- what can be migrated
def K(path, label, model, fields, fk=None, key=("name",)):
    return dict(path=path, label=label, model=model, fields=fields.split(), fk=fk or {}, key=key)


KINDS = {
    "organizations": K("/api/v2/organizations/", "organization", "Organization", "name description max_hosts"),
    "credential_types": K("/api/v2/credential_types/", "credential type", "CredentialType",
                          "name description kind inputs injectors"),
    "teams": K("/api/v2/teams/", "team", "Team", "name description",
               {"organization": "organizations"}, ("name", "organization")),
    "credentials": K("/api/v2/credentials/", "credential", "Credential", "name description",
                     {"organization": "organizations", "credential_type": "credential_types"},
                     ("name", "credential_type", "organization")),
    "projects": K("/api/v2/projects/", "project", "Project",
                  "name description scm_type scm_url scm_branch scm_refspec scm_clean scm_delete_on_update "
                  "scm_track_submodules scm_update_on_launch scm_update_cache_timeout allow_override timeout local_path",
                  {"organization": "organizations", "credential": "credentials"}, ("name", "organization")),
    "inventories": K("/api/v2/inventories/", "inventory", "Inventory", "name description kind host_filter variables",
                     {"organization": "organizations"}, ("name", "organization")),
    "inventory_sources": K("/api/v2/inventory_sources/", "inventory source", "InventorySource",
                           "name description source source_path source_vars source_regions instance_filters group_by "
                           "enabled_var enabled_value host_filter overwrite overwrite_vars timeout verbosity "
                           "update_on_launch update_cache_timeout",
                           {"inventory": "inventories", "credential": "credentials", "source_project": "projects"},
                           ("name", "inventory")),
    "job_templates": K("/api/v2/job_templates/", "job template", "JobTemplate",
                       "name description job_type playbook scm_branch forks limit verbosity extra_vars job_tags "
                       "force_handlers skip_tags start_at_task timeout use_fact_cache host_config_key "
                       "ask_scm_branch_on_launch ask_diff_mode_on_launch ask_variables_on_launch ask_limit_on_launch "
                       "ask_tags_on_launch ask_skip_tags_on_launch ask_job_type_on_launch ask_verbosity_on_launch "
                       "ask_inventory_on_launch ask_credential_on_launch survey_enabled become_enabled diff_mode "
                       "allow_simultaneous job_slice_count",
                       {"inventory": "inventories", "project": "projects"}, ("name", "project")),
    "schedules": K("/api/v2/schedules/", "schedule", "Schedule", "name description rrule enabled extra_data",
                   {"unified_job_template": None}, ("name", "unified_job_template")),
}
ORDER = ["organizations", "credential_types", "teams", "credentials", "projects", "inventories",
         "inventory_sources", "job_templates", "schedules"]
ROLE_KINDS = ("organizations", "teams", "credentials", "projects", "inventories", "job_templates")
ROLE_RES = {"credential": "credentials", "project": "projects", "inventory": "inventories",
            "job_template": "job_templates"}
UJT = {"job": "job_templates", "project_update": "projects", "inventory_update": "inventory_sources"}
WHAT = {"orgs": ("organizations",), "teams": ("teams",), "types": ("credential_types",),
        "credentials": ("credentials",), "projects": ("projects",), "inventories": ("inventories", "inventory_sources"),
        "templates": ("job_templates",), "schedules": ("schedules",)}
DEFAULT_WHAT = "orgs,teams,types,credentials,projects,inventories,templates,schedules,access"


def is_custom(t):
    return t.get("managed") is False or t.get("managed_by_tower") is False


def fields(t):
    return (t.get("inputs") or {}).get("fields") or []


def sig(t):
    return sorted((f["id"], bool(f.get("secret"))) for f in fields(t))


# ----------------------------------------------------------------------------- secrets (run on the Tower VM)
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
    """{cred_id: [field defs]} -> {cred_id: {field: plain text}} via awx-manage on this VM."""
    mng = os.environ.get("TOWER_AWX_MANAGE") or "/usr/bin/awx-manage"
    sudo = [] if getpass.getuser() == "awx" else ["sudo", "-n", "-u", "awx"]
    cmd = sudo + ["env", "GIT_PYTHON_REFRESH=quiet", mng, "shell"]
    try:
        p = subprocess.run(cmd, input=CODE.replace("__REQ__", repr(json.dumps(req))), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Fail("cannot run awx-manage here (%s). Run this on the Tower VM; set TOWER_AWX_MANAGE if it "
                   "is not /usr/bin/awx-manage" % type(e).__name__)
    if p.returncode:
        raise Fail("awx-manage failed (is 'sudo -n -u awx' allowed without a password?): "
                   + " | ".join(p.stderr.splitlines()[-3:]))
    lines = [l[13:] for l in p.stdout.splitlines() if l.startswith("MIGRATE_JSON:")]
    if len(lines) != 1:
        raise Fail("unexpected output from awx-manage")
    return json.loads(lines[0])


# ----------------------------------------------------------------------------- the migration
class Mig(object):
    def __init__(self, a, src, dst, me):
        self.a, self.src, self.dst, self.me = a, src, dst, me
        self.go = bool(a.execute)
        self.tty = sys.stdin.isatty()
        self.S, self.D = {}, {}                # source objects / destination lists, by kind
        self.want, self.roots, self.done = {}, set(), {}
        self.left = []                         # (what, name, why) not migrated
        self.jt_creds, self.plain, self.cred_err = {}, {}, {}
        self.creators, self.fp, self.new_users, self.fresh = [], [], [], set()
        self.mode, self.du, self.skip_users = {}, {}, set()
        self.n = {"created": 0, "existing": 0, "mismatch": 0}

    # ---- reading ----
    def load(self):
        say("Reading Tower ...")
        for kind, spec in KINDS.items():
            self.S[kind] = dict((o["id"], o) for o in self.src.all(spec["path"]))
        self.S["users"] = dict((u["id"], u) for u in self.src.all("/api/v2/users/"))
        if self.dst:
            self.du = dict((u["username"], u["id"]) for u in self.dst.all("/api/v2/users/"))

    def dlist(self, kind):
        if kind not in self.D:
            self.D[kind] = self.dst.all(KINDS[kind]["path"]) if self.dst else []
        return self.D[kind]

    def label(self, kind, o):
        org = (self.S["organizations"].get(o.get("organization")) or {}).get("name")
        return "%s/%s" % (org, o["name"]) if org and kind != "organizations" else o["name"]

    def ujt_kind(self, o):
        sf = (o.get("summary_fields") or {}).get("unified_job_template") or {}
        return UJT.get(sf.get("unified_job_type"))

    def pick(self, kind, specs):
        out = []
        for sp in specs:
            hit = [o for o in self.S[kind].values() if sp in (o["name"], self.label(kind, o))]
            if not hit:
                raise Fail("no %s named '%s' on Tower (use --list to see the names)" % (KINDS[kind]["label"], sp))
            if len(hit) > 1:
                say("  note: '%s' matches %d %ss - using all of them (write ORG/NAME to narrow it)"
                    % (sp, len(hit), KINDS[kind]["label"]))
            out += hit
        return out

    def show_list(self):
        for kind in ORDER:
            if kind == "inventory_sources":
                continue
            items = sorted(self.label(kind, o) + ("   [%s]" % self.S["credential_types"][o["credential_type"]]["name"]
                                                  if kind == "credentials" else "") for o in self.S[kind].values())
            print("\n%s (%d)" % (kind.replace("_", " ").upper(), len(items)))
            for i in items:
                print("  " + i)
        print("\nUSERS (%d)" % len(self.S["users"]))
        for u in sorted(self.S["users"].values(), key=lambda u: u["username"]):
            print("  %s%s" % (u["username"], "   [LDAP/SSO]" if u.get("ldap_dn") or u.get("external_account") else ""))

    # ---- choosing what to migrate ----
    def choose_scope(self):
        a = self.a
        i = menu("What do you want to migrate?", [
            "Everything (organizations, teams, credentials, projects, inventories, templates, schedules, access)",
            "Selected organizations (with their teams, credentials, projects, inventories, templates)",
            "Selected teams (their members, plus everything those teams can access)",
            "Selected credentials only",
            "Selected projects, inventories or job templates",
            "Let me choose the resource types"])

        def names(kind):
            return sorted(self.label(kind, o) for o in self.S[kind].values())
        if i == 1:
            a.org = multi("Organizations on Tower:", names("organizations"))
        elif i == 2:
            a.team = multi("Teams on Tower:", names("teams"))
        elif i == 3:
            a.credential = multi("Credentials on Tower:", names("credentials"))
        elif i == 4:
            for attr, kind in (("project", "projects"), ("inventory", "inventories"), ("template", "job_templates")):
                setattr(a, attr, multi("%ss on Tower (ENTER to skip):" % KINDS[kind]["label"].capitalize(), names(kind)))
        elif i == 5:
            opts = ["orgs", "teams", "types", "credentials", "projects", "inventories", "templates", "schedules",
                    "access", "users"]
            a.what = ",".join(multi("Resource types ('access' = memberships and permissions; "
                                    "'users' = create missing local users):", opts))
        if i == 0 or 1 <= i <= 4:
            if yesno("\nAlso create users that are missing on AWX? (they get random passwords)"):
                a.what += ",users"
        if i != 0 and not (a.org or a.team or a.credential or a.project or a.inventory or a.template) and i != 5:
            raise Stop()

    def setup(self):
        a = self.a
        items = set(x.strip() for x in a.what.split(",") if x.strip())
        if "all" in items:
            items = set(WHAT) | {"users", "access"}
        bad = items - set(WHAT) - {"users", "access"}
        if bad:
            raise Fail("unknown resource type(s): %s (valid: %s, users, access, all)" % (", ".join(sorted(bad)), ", ".join(WHAT)))
        self.kinds = set(k for i in items if i in WHAT for k in WHAT[i])
        self.users_on, self.access_on = "users" in items, "access" in items

    def build_scope(self):
        a = self.a
        sel = any([a.org, a.team, a.credential, a.project, a.inventory, a.template])
        roots = []
        if not sel:
            for kind in ORDER:
                if kind in self.kinds and kind != "schedules":
                    roots += [(kind, i) for i, o in self.S[kind].items() if kind != "credential_types" or is_custom(o)]
        for o in self.pick("organizations", a.org):
            roots.append(("organizations", o["id"]))
            for kind in ("teams", "credentials", "projects", "inventories"):
                if kind in self.kinds:
                    roots += [(kind, i) for i, x in self.S[kind].items() if x.get("organization") == o["id"]]
        if a.org and "job_templates" in self.kinds:
            projs = set(i for k, i in roots if k == "projects")
            invs = set(i for k, i in roots if k == "inventories")
            roots += [("job_templates", i) for i, j in self.S["job_templates"].items()
                      if j.get("project") in projs or j.get("inventory") in invs]
        for t in self.pick("teams", a.team):
            roots.append(("teams", t["id"]))
            for r in self.src.all("/api/v2/teams/%s/roles/" % t["id"]):
                sf = r.get("summary_fields") or {}
                kind = ROLE_RES.get(sf.get("resource_type"))
                if kind and kind in self.kinds and sf.get("resource_id") in self.S[kind]:
                    roots.append((kind, sf["resource_id"]))
        for kind, specs in (("credentials", a.credential), ("projects", a.project),
                            ("inventories", a.inventory), ("job_templates", a.template)):
            roots += [(kind, o["id"]) for o in self.pick(kind, specs)]
        self.roots = set(roots)
        for k, i in roots:
            self.add(k, i)
        if "schedules" in self.kinds:
            for i, sch in self.S["schedules"].items():
                uk = self.ujt_kind(sch)
                if uk and (not sel or (uk, sch.get("unified_job_template")) in self.want):
                    self.add("schedules", i)
        return sel

    def add(self, kind, sid):
        o = self.S[kind].get(sid)
        if o is None or (kind, sid) in self.want:
            return
        self.want[(kind, sid)] = o
        for f, fk in KINDS[kind]["fk"].items():
            fk = fk or self.ujt_kind(o)
            if fk and o.get(f):
                self.add(fk, o[f])
        if kind == "job_templates":
            self.jt_creds[sid] = [c["id"] for c in self.src.all("/api/v2/job_templates/%s/credentials/" % sid)]
            for c in self.jt_creds[sid]:
                self.add("credentials", c)
        if kind == "inventories":
            for i, s in self.S["inventory_sources"].items():
                if s.get("inventory") == sid:
                    self.add("inventory_sources", i)

    def decrypt(self):
        req = dict((str(i), fields(self.S["credential_types"][o["credential_type"]]))
                   for (k, i), o in self.want.items() if k == "credentials")
        if not req:
            return
        say("Reading %d credential secret(s) on this Tower VM ..." % len(req))
        try:
            vals = local_secrets(req)
        except Fail as e:
            say("  ! could not read secrets: %s" % e)
            if self.go and not self.a.yes and not yesno("Continue without any credentials?"):
                raise Stop()
            vals = {}
        for cid in req:
            cred = self.S["credentials"][int(cid)]
            t = self.S["credential_types"][cred["credential_type"]]
            v = vals.get(cid)
            if v is None or "__error__" in v:
                self.cred_err[int(cid)] = "its secrets could not be read (%s)" % ((v or {}).get("__error__") or "no data")
                continue
            allowed = set(f["id"] for f in fields(t))
            inputs = dict((k, x) for k, x in v.items() if k in allowed and x is not None)
            missing = [r for r in (t.get("inputs") or {}).get("required", []) if inputs.get(r) in (None, "")]
            if missing:
                self.cred_err[int(cid)] = "required field(s) have no value on Tower: " + ", ".join(missing)
            else:
                self.plain[int(cid)] = inputs

    # ---- doing it ----
    def run(self):
        self.setup()
        sel = self.build_scope()
        counts = {}
        for k, _ in self.want:
            counts[k] = counts.get(k, 0) + 1
        say("\nScope: %s%s" % ("only what you selected, plus what it needs" if sel else "everything",
                               "" if self.access_on else " (access/permissions NOT included)"))
        say("  " + (", ".join("%d %s" % (counts[k], k.replace("_", " ")) for k in ORDER if k in counts) or "nothing"))
        if not self.want and not (self.users_on and not sel):
            raise Fail("nothing to migrate with these choices")
        self.decrypt()
        say("\nMODE: %s" % ("MIGRATE - changes will be made on AWX" if self.go else "DRY RUN - nothing will be changed"))
        if self.go and not self.a.yes and not yesno("Start now? You will be asked before anything except credentials is created"):
            raise Stop()
        for kind in ORDER:
            todo = [k for k in list(self.want) if k[0] == kind]
            if todo:
                say("\n== %s ==" % kind.replace("_", " ").upper())
            for k in todo:
                self.dest(*k)
        if self.users_on and not sel:
            say("\n== USERS ==")
            for sid in self.S["users"]:
                self.user(sid)

    def dest(self, kind, sid):
        """Destination id for a source object; creates it (after confirmation) when missing. None = not migrated."""
        if (kind, sid) in self.done:
            return self.done[(kind, sid)]
        self.done[(kind, sid)] = None
        o = self.S[kind].get(sid)
        if o is None:
            return None
        name = self.label(kind, o)
        try:
            did = self.make(kind, o, name)
        except Fail as e:
            did = None
            self.left.append((KINDS[kind]["label"], name, str(e)))
            say("  - NOT migrated: %s '%s' (%s)" % (KINDS[kind]["label"], name, e))
        self.done[(kind, sid)] = did
        return did

    def payload(self, kind, o, resolve):
        p = dict((f, o[f]) for f in KINDS[kind]["fields"] if o.get(f) is not None)
        for f, fk in KINDS[kind]["fk"].items():
            v = o.get(f)
            if v is None:
                p[f] = None
                continue
            fk = fk or self.ujt_kind(o)
            if not fk:
                raise Fail("this schedule belongs to a template type that is not supported")
            d = resolve(fk, v)
            if d is None:
                raise Fail("needs %s '%s', which was not migrated"
                           % (KINDS[fk]["label"], self.label(fk, self.S[fk][v]) if v in self.S[fk] else v))
            p[f] = d
        return p

    def make(self, kind, o, name):
        spec = KINDS[kind]
        label = spec["label"]
        p = self.payload(kind, o, self.dest)
        if kind == "credentials":
            if o["id"] not in self.plain:
                raise Fail(self.cred_err.get(o["id"], "its secrets were not read"))
            p["inputs"] = self.plain[o["id"]]
        if kind == "projects" and p.get("scm_type"):
            p.pop("local_path", None)
        if kind == "schedules" and not self.a.enable_schedules:
            p["enabled"] = False
        if kind == "credential_types":
            ex = next((d for d in self.dlist(kind) if d["name"] == o["name"] and is_custom(d) == is_custom(o)), None)
            if ex and is_custom(o) and sig(o) != sig(ex):
                raise Fail("AWX already has a credential type with this name but different fields")
            if not ex and not is_custom(o):
                raise Fail("this built-in credential type does not exist on AWX")
        else:
            ex = next((d for d in self.dlist(kind) if all(d.get(k) == p.get(k) for k in spec["key"])), None)
        if ex:
            say("  = already on AWX: %s '%s'" % (label, name))
            self.n["existing"] += 1
            self.after(kind, o, ex["id"], False, p)
            return ex["id"]
        if kind == "job_templates":
            self.wait_project(p.get("project"))
        post = dict((k, v) for k, v in p.items() if v is not None)
        if kind == "credentials" and "organization" not in post:
            post["user"] = self.me["id"]
        if self.go and kind != "credentials" and not self.confirm(label, "%s '%s'" % (label, name)):
            raise Fail("not created: you answered no")
        say("  + %s %s '%s'" % ("creating" if self.go else "would create", label, name))
        did = NEW
        if self.go:
            r = self.dst.call("POST", spec["path"], post)
            did = r["id"]
            self.dlist(kind).append(r)
            sf = o.get("summary_fields") or {}
            self.creators.append({"model": spec["model"], "id": did, "name": o.get("name"),
                                  "created_by": (sf.get("created_by") or {}).get("username"),
                                  "modified_by": (sf.get("modified_by") or {}).get("username"),
                                  "created": o.get("created"), "modified": o.get("modified")})
        self.n["created"] += 1
        self.after(kind, o, did, True, p)
        return did

    def confirm(self, label, text):
        if self.a.yes:
            return True
        if self.mode.get(label) is not None:
            return self.mode[label]
        while True:
            r = ask("  Create %s on AWX? [y]es / [n]o / [a]ll %ss / [s]kip all %ss / [q]uit"
                    % (text, label, label)).lower()
            if r in ("y", "yes"):
                return True
            if r in ("n", "no", ""):
                return False
            if r in ("a", "all"):
                self.mode[label] = True
                return True
            if r in ("s", "skip"):
                self.mode[label] = False
                return False
            if r in ("q", "quit"):
                raise Stop()

    def after(self, kind, o, did, created, p):
        if kind == "credentials":
            self.verify(o, did, p)
        if kind == "projects" and created and did != NEW:
            self.fresh.add(did)
        if kind == "job_templates" and created:
            self.jt_extras(o, did)
        if kind == "inventories":
            self.inv_content(o, did)
        if self.access_on and kind in ROLE_KINDS and (kind not in ("organizations", "teams") or (kind, o["id"]) in self.roots):
            self.replay(kind, o["id"], did, self.label(kind, o))

    def wait_project(self, did):
        if not self.go or did not in self.fresh:
            return
        say("  ... waiting for the new project to finish syncing (job templates need its playbooks)")
        end, st = time.time() + 300, "?"
        while time.time() < end:
            st = self.dst.call("GET", "/api/v2/projects/%s/" % did).get("status")
            if st not in ("new", "pending", "waiting", "running"):
                break
            time.sleep(3)
        self.fresh.discard(did)
        if st != "successful":
            say("  ! project sync ended as '%s' - templates that use it may be refused" % st)

    def verify(self, o, did, p):
        """Read the AWX copy back; remember the secrets for the optional fingerprint file."""
        if not self.go or did == NEW:
            return
        t = self.S["credential_types"][o["credential_type"]]
        secret = set(f["id"] for f in fields(t) if f.get("secret"))
        want = p["inputs"]
        d = self.dst.call("GET", "/api/v2/credentials/%s/" % did)
        bad = [] if (d["name"], d["credential_type"], d["organization"]) == (p["name"], p["credential_type"], p.get("organization")) else ["name/type/org"]
        for k, v in want.items():
            got = (d.get("inputs") or {}).get(k)
            if (k in secret and got != "$encrypted$") or (k not in secret and got != v):
                bad.append(k)
        self.fp.append((did, p["name"], dict((k, want[k]) for k in secret if k in want)))
        if bad:
            self.n["mismatch"] += 1
            say("  ! read-back MISMATCH on credential '%s': %s" % (p["name"], ", ".join(bad)))
        else:
            say("    verified: the AWX copy matches (secrets are set)")

    def jt_extras(self, o, did):
        creds = self.jt_creds.get(o["id"], [])
        survey = self.src.call("GET", "/api/v2/job_templates/%s/survey_spec/" % o["id"])
        has_survey = bool(survey and survey.get("spec"))
        if not self.go or did == NEW:
            say("    would attach %d credential(s)%s" % (len(creds), " and the survey" if has_survey else ""))
            return
        for cid in creds:
            dc = self.dest("credentials", cid)
            cname = self.label("credentials", self.S["credentials"][cid])
            if dc is None:
                self.left.append(("credential on template", "%s -> %s" % (o["name"], cname), "credential was not migrated"))
                continue
            try:
                self.dst.call("POST", "/api/v2/job_templates/%s/credentials/" % did, {"id": dc})
            except Fail as e:
                self.left.append(("credential on template", "%s -> %s" % (o["name"], cname), str(e)))
                say("  ! could not attach credential '%s': %s" % (cname, e))
        if has_survey:
            try:
                self.dst.call("POST", "/api/v2/job_templates/%s/survey_spec/" % did, survey)
            except Fail as e:
                self.left.append(("survey", o["name"], str(e)))
                say("  ! could not copy the survey: %s" % e)

    def inv_content(self, o, did):
        if o.get("kind") == "smart":
            return
        sid = o["id"]
        groups = [g for g in self.src.all("/api/v2/inventories/%s/groups/" % sid) if not g.get("has_inventory_sources")]
        hosts = [h for h in self.src.all("/api/v2/inventories/%s/hosts/" % sid) if not h.get("has_inventory_sources")]
        have_g = dict((g["name"], g["id"]) for g in self.dst.all("/api/v2/inventories/%s/groups/" % did)) if did != NEW else {}
        have_h = dict((h["name"], h["id"]) for h in self.dst.all("/api/v2/inventories/%s/hosts/" % did)) if did != NEW else {}
        new_g = [g for g in groups if g["name"] not in have_g]
        new_h = [h for h in hosts if h["name"] not in have_h]
        if not (new_g or new_h):
            return
        what = "%d host(s) and %d group(s) in inventory '%s'" % (len(new_h), len(new_g), o["name"])
        if not self.go or did == NEW:
            say("  + would create %s" % what)
            return
        if not self.confirm("inventory content", what):
            self.left.append(("hosts/groups", o["name"], "not created: you answered no"))
            return
        say("  + creating %s" % what)
        fails = []
        for g in new_g:
            try:
                have_g[g["name"]] = self.dst.call("POST", "/api/v2/inventories/%s/groups/" % did, {
                    "name": g["name"], "description": g.get("description") or "", "variables": g.get("variables") or ""})["id"]
            except Fail as e:
                fails.append(str(e))
        for n, h in enumerate(new_h, 1):
            try:
                have_h[h["name"]] = self.dst.call("POST", "/api/v2/inventories/%s/hosts/" % did, {
                    "name": h["name"], "description": h.get("description") or "", "enabled": h.get("enabled", True),
                    "instance_id": h.get("instance_id") or "", "variables": h.get("variables") or ""})["id"]
            except Fail as e:
                fails.append(str(e))
            if n % 200 == 0:
                say("    ... %d/%d hosts" % (n, len(new_h)))
        for g in new_g:
            if g["name"] not in have_g:
                continue
            for rel, have in (("hosts", have_h), ("children", have_g)):
                for m in self.src.all("/api/v2/groups/%s/%s/" % (g["id"], rel)):
                    if m["name"] in have:
                        try:
                            self.dst.call("POST", "/api/v2/groups/%s/%s/" % (have_g[g["name"]], rel), {"id": have[m["name"]]})
                        except Fail as e:
                            fails.append(str(e))
        if fails:
            self.left.append(("hosts/groups", o["name"], "%d problem(s), first: %s" % (len(fails), fails[0])))
            say("  ! %d problem(s) in inventory '%s' (first: %s)" % (len(fails), o["name"], fails[0]))

    def user(self, sid):
        u = self.S["users"].get(sid)
        if not u:
            return None
        name = u["username"]
        if name in self.du:
            return self.du[name]
        if name in self.skip_users:
            return None
        why = None
        if u.get("ldap_dn") or u.get("external_account"):
            why = "LDAP/SSO user: appears on AWX at first login (run again afterwards to attach access)"
        elif not self.users_on:
            why = "does not exist on AWX and user creation is off (choose 'users' to create it)"
        elif self.go and not self.confirm("user", "user '%s'%s" % (name, " (SUPERUSER)" if u.get("is_superuser") else "")):
            why = "not created: you answered no"
        if not why:
            say("  + %s user '%s'" % ("creating" if self.go else "would create", name))
            if not self.go:
                self.du[name] = NEW
                return NEW
            pw = secrets.token_urlsafe(12)
            body = dict((k, u.get(k) or "") for k in ("username", "first_name", "last_name", "email"))
            body.update(password=pw, is_superuser=bool(u.get("is_superuser")), is_system_auditor=bool(u.get("is_system_auditor")))
            try:
                self.du[name] = self.dst.call("POST", "/api/v2/users/", body)["id"]
                self.new_users.append((name, pw))
                return self.du[name]
            except Fail as e:
                why = str(e)
        self.skip_users.add(name)
        self.left.append(("user", name, why))
        say("  - user '%s' skipped: %s" % (name, why))
        return None

    def replay(self, kind, sid, did, label):
        """Copy who has which role (users and teams) on a source object to its AWX twin. Additive only."""
        apply = self.go and did != NEW
        path = KINDS[kind]["path"]
        droles = dict((r["name"], r["id"]) for r in self.dst.all("%s%s/object_roles/" % (path, did))) if apply else None
        for r in self.src.all("%s%s/object_roles/" % (path, sid)):
            if droles is not None and r["name"] not in droles:
                continue
            holders = [("users", u["username"], self.user(u["id"])) for u in self.src.all("/api/v2/roles/%s/users/" % r["id"])]
            holders += [("teams", t["name"], self.dest("teams", t["id"])) for t in self.src.all("/api/v2/roles/%s/teams/" % r["id"])]
            for hk, who, target in holders:
                if target is None:
                    continue
                say("    %s %s -> %s %s on %s" % ("grant" if apply else "would grant", r["name"], hk[:-1], who, label))
                if apply and target != NEW:
                    try:
                        self.dst.call("POST", "/api/v2/%s/%s/roles/" % (hk, target), {"id": droles[r["name"]]})
                    except Fail as e:
                        self.left.append(("access", "%s on %s" % (who, label), str(e)))

    # ---- inspect one credential ----
    def peek(self, kind, sid):
        o = self.S[kind][sid]
        ex = next((d["id"] for d in self.dlist(kind) if d["name"] == o["name"]), None) if self.dst else None
        return ex if ex else "<%s '%s': %s>" % (KINDS[kind]["label"], o["name"], "will be created" if self.dst else "id on AWX")

    def inspect(self, spec):
        if spec == "?":
            picked = multi("Pick a credential to inspect:", sorted(self.label("credentials", o) for o in self.S["credentials"].values()))
            if not picked:
                raise Stop()
            spec = picked[0]
        for o in self.pick("credentials", [spec]):
            t = self.S["credential_types"][o["credential_type"]]
            vals = local_secrets({str(o["id"]): fields(t)}).get(str(o["id"]), {})
            if "__error__" in vals:
                raise Fail("could not read the secrets of '%s' (%s)" % (o["name"], vals["__error__"]))
            print("\n" + "=" * 70 + "\nCREDENTIAL %s   (type: %s)\nNOTHING is changed by this view; plain-text secrets follow.\n" % (self.label("credentials", o), t["name"]) + "=" * 70)
            show("1. Tower API  GET /api/v2/credentials/%s/   (Tower masks secrets)" % o["id"],
                 self.src.call("GET", "/api/v2/credentials/%s/" % o["id"]))
            show("2. Plain-text values read on this VM (secret fields: %s)" % ", ".join(f["id"] for f in fields(t) if f.get("secret")), vals)
            p = self.payload("credentials", o, self.peek)
            p["inputs"] = vals
            if p.get("organization") is None:
                p["user"] = "<the AWX token user>"
            show("3. What would be sent to AWX   POST /api/v2/credentials/", dict((k, v) for k, v in p.items() if v is not None))
            if self.dst:
                ex = next((d for d in self.dlist("credentials") if d["name"] == o["name"]), None)
                if ex:
                    show("4. A credential with this name already on AWX (AWX masks secrets)",
                         self.dst.call("GET", "/api/v2/credentials/%s/" % ex["id"]))
                else:
                    print("\n4. No credential with this name exists on AWX yet.")

    # ---- the end of a run ----
    def finish(self, prompt=True):
        a = self.a
        ask_ok = prompt and self.tty and not a.yes
        say("\n" + "=" * 70)
        say("%s: %d %s, %d already on AWX, %d not migrated%s" % (
            "DONE" if self.go else "DRY RUN", self.n["created"], "created" if self.go else "would be created",
            self.n["existing"], len(self.left), ", %d read-back mismatch(es)" % self.n["mismatch"] if self.n["mismatch"] else ""))
        if not self.go:
            say("Nothing was changed. Run again and choose 'Migrate' (--execute) to apply this.")
        ts = time.strftime("%Y%m%d_%H%M%S")
        if self.left:
            lines = ["%-22s %s  --  %s" % (k, n, why) for k, n, why in self.left]
            c = "f" if a.report else (ask("\n%d item(s) were NOT migrated. [s]how the list / save to a [f]ile / [b]oth / [n]o" % len(lines)).lower()[:1] or "n") if ask_ok else "s"
            if c in ("s", "b"):
                say("\nNOT migrated:\n  " + "\n  ".join(lines))
            if c in ("f", "b"):
                path = a.report or "not_migrated_%s.txt" % ts
                with private_file(path) as f:
                    f.write("Items NOT migrated (%d)\n\n%s\n" % (len(lines), "\n".join(lines)))
                say("Saved: " + path)
        if not self.go:
            return
        if self.new_users:
            with private_file("new_user_passwords.txt", "a") as f:
                f.writelines("%s %s\n" % u for u in self.new_users)
            say("\n%d user(s) were created with random passwords. They are in new_user_passwords.txt - hand them out, then delete the file." % len(self.new_users))
        if self.creators:
            path = a.creator_map or (("creator_map_%s.json" % ts) if ask_ok and yesno("\nSave a creator map? (lets awx_set_creators.py restore the original creators on AWX)") else None)
            if path:
                with private_file(path) as f:
                    json.dump(self.creators, f, indent=1)
                say("Saved: %s  (then run awx_set_creators.py on the AWX host)" % path)
        if self.fp:
            path = a.fingerprints or (("fingerprints_%s.json" % ts) if ask_ok and yesno("\nWrite secret fingerprints? (lets you prove on AWX that every secret matches, without showing it)") else None)
            if path:
                key = os.urandom(16).hex()
                with private_file(path) as f:
                    json.dump([{"id": i, "name": n, "fields": dict(
                        (k, hmac.new(key.encode(), (k + ":" + str(v)).encode(), hashlib.sha256).hexdigest()) for k, v in s.items())}
                        for i, n, s in self.fp], f, indent=1)
                print("Saved: %s\nVERIFY KEY (write it down, it is not saved anywhere): %s\n(then run awx_verify_secrets.py on the AWX host)" % (path, key))


# ----------------------------------------------------------------------------- program flow
def connect(name, url, token, a):
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "https://" + url
    if url.endswith("/api/v2"):
        url = url[:-7]
    if not (url.startswith("https://") or (a.allow_http and url.startswith("http://"))):
        raise Fail("the %s URL must start with https:// (plain http would send secrets unencrypted; --allow-http overrides)" % name)
    if not token:
        raise Fail("no %s token given" % name)
    api = API(name, url, token, a.insecure)
    ping = api.call("GET", "/api/v2/ping/", timeout=15)
    me = api.call("GET", "/api/v2/me/")["results"][0]
    say("  %s: connected to %s (version %s) as '%s'" % (name, url, ping.get("version"), me["username"]))
    if not me.get("is_superuser"):
        say("  note: '%s' is not a superuser on %s, so some items may be refused" % (me["username"], name))
    return api, me


BANNER = """
==========================================================================
  Ansible Tower  ->  AWX  migration tool
  Nothing is changed until you pick "Migrate". Nothing is ever overwritten
  or deleted on AWX, so you can run it again safely.
=========================================================================="""


def app(a):
    tty = sys.stdin.isatty()
    guided = not (a.list or a.inspect or a.dry_run or a.execute)
    if guided and not tty:
        raise Fail("pick an action: --list, --inspect NAME, --dry-run or --execute (or run from a terminal for the guided menu)")
    if a.execute and not a.yes and not tty:
        raise Fail("--execute asks for confirmations, so it needs a terminal; add --yes to confirm everything automatically")
    if guided:
        print(BANNER)
        i = menu("What would you like to do?", [
            "Dry run   - show everything that WOULD be migrated; changes nothing (do this first)",
            "Migrate   - really migrate; you are asked before anything except credentials is created",
            "Inspect a credential - show its plain-text secrets and the exact JSON that would be sent to AWX",
            "List what exists on Tower",
            "Quit"])
        if i == 4:
            return 0
        a.dry_run, a.execute, a.list = i == 0, i == 1, i == 3
        a.inspect = "?" if i == 2 else None
        if not a.insecure:
            a.insecure = yesno("\nSkip TLS certificate checks? (say yes if the servers have self-signed or no certificates)")
    src_url = a.src_url or ask("Tower URL (this VM)", "https://127.0.0.1")
    st = os.environ.get("TOWER_TOKEN") or ask("Tower token (typing is hidden)", secret=True)
    dst_url = "" if a.list else a.dest_url
    if not a.list and not dst_url and (guided or not a.inspect):
        dst_url = ask("AWX URL" + (" (ENTER to skip AWX)" if a.inspect else ""))
    if not a.list and not dst_url and not a.inspect:
        raise Fail("the AWX URL is required (--dest-url)")
    dt = (os.environ.get("AWX_TOKEN") or ask("AWX token (typing is hidden)", secret=True)) if dst_url else None
    say("\nConnecting ...")
    src, _ = connect("Tower", src_url, st, a)
    dst, me = connect("AWX", dst_url, dt, a) if dst_url else (None, None)
    mig = Mig(a, src, dst, me)
    mig.load()
    if a.list:
        mig.show_list()
        return 0
    if a.inspect:
        mig.inspect(a.inspect)
        return 0
    if guided:
        mig.choose_scope()
    try:
        mig.run()
    except (Stop, KeyboardInterrupt):
        say("\nStopped early. What was done so far is summarised below; running again is safe.")
        mig.finish(prompt=False)
        return 130
    mig.finish()
    return 3 if mig.left or mig.n["mismatch"] else 0


def parse():
    ap = argparse.ArgumentParser(
        prog="tower_awx_migrate_v3.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Migrate Ansible Tower 3.8 to AWX 24. Run on the Tower VM. With no action option you get a guided menu.",
        epilog="""examples:
  python3 tower_awx_migrate_v3.py                         guided menu (easiest)
  python3 tower_awx_migrate_v3.py --list                  see what exists on Tower
  python3 tower_awx_migrate_v3.py --dest-url https://AWX:9443 --insecure --dry-run
  python3 tower_awx_migrate_v3.py --dest-url https://AWX:9443 --insecure --execute --org Acme
  python3 tower_awx_migrate_v3.py --inspect "Acme/my-test-cred"       show secrets and the JSON sent

tokens: set TOWER_TOKEN and AWX_TOKEN to avoid the prompts.
resource types for --what: orgs teams types credentials projects inventories templates schedules access users all
  default = everything except users. 'access' = memberships and permissions.
not migrated: workflow templates, notifications, labels, instance groups, execution environments, applications,
  settings, job history, and playbook files of manual projects.""")
    g = ap.add_argument_group("connection (anything left out is asked for)")
    g.add_argument("--src-url", help="Tower URL; from the Tower VM usually https://127.0.0.1")
    g.add_argument("--dest-url", help="AWX URL, e.g. https://10.20.1.4:9443")
    g.add_argument("--insecure", action="store_true", help="skip TLS certificate checks (self-signed / no certificates)")
    g.add_argument("--allow-http", action="store_true", help="allow plain http:// URLs (secrets travel unencrypted)")
    g = ap.add_argument_group("what to do (leave all out for the guided menu)")
    g.add_argument("--list", action="store_true", help="list the orgs, teams, credentials ... on Tower and exit")
    g.add_argument("--inspect", metavar="CREDENTIAL", help="show one credential's plain-text secrets and the JSON that would be sent to AWX")
    g.add_argument("--dry-run", action="store_true", help="show what would be migrated, change nothing")
    g.add_argument("--execute", action="store_true", help="really migrate (asks before creating anything except credentials)")
    g = ap.add_argument_group("what to migrate (default: everything except users)")
    g.add_argument("--what", default=DEFAULT_WHAT, help="comma list of resource types (see below)")
    g.add_argument("--org", action="append", default=[], metavar="NAME", help="an organization with its teams, credentials, projects, inventories, templates (repeatable)")
    g.add_argument("--team", action="append", default=[], metavar="NAME", help="a team with its members and everything it can access (repeatable)")
    g.add_argument("--credential", action="append", default=[], metavar="NAME", help="only this credential (repeatable); NAME or ORG/NAME")
    g.add_argument("--project", action="append", default=[], metavar="NAME", help="only this project (repeatable)")
    g.add_argument("--inventory", action="append", default=[], metavar="NAME", help="only this inventory with its hosts and groups (repeatable)")
    g.add_argument("--template", action="append", default=[], metavar="NAME", help="only this job template (repeatable)")
    g.add_argument("--enable-schedules", action="store_true", help="keep schedules enabled (default: created disabled so both servers do not run jobs)")
    g = ap.add_argument_group("output (nothing is written to disk unless you ask)")
    g.add_argument("--yes", action="store_true", help="answer yes to every confirmation (for unattended runs)")
    g.add_argument("--log", metavar="FILE", help="also save the screen output to FILE (contains no secrets)")
    g.add_argument("--report", metavar="FILE", help="save the list of items that were not migrated")
    g.add_argument("--creator-map", metavar="FILE", help="save original creators, for awx_set_creators.py")
    g.add_argument("--fingerprints", metavar="FILE", help="save secret fingerprints, for awx_verify_secrets.py")
    g.add_argument("--debug", action="store_true", help="show technical details when something unexpected happens")
    return ap.parse_args()


def main():
    global LOG
    a = parse()
    try:
        if a.log:
            LOG = private_file(a.log, "a")
        return app(a)
    except Stop:
        say("\nStopped. Nothing else was changed.")
        return 130
    except KeyboardInterrupt:
        say("\nInterrupted. Nothing is rolled back, and running again is safe.")
        return 130
    except Fail as e:
        say("\nERROR: %s" % e)
        return 2
    except Exception as e:
        if a.debug:
            traceback.print_exc()
        say("\nUNEXPECTED ERROR: %s: %s\n(Add --debug for details. Running again is safe: nothing is overwritten.)" % (type(e).__name__, e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
