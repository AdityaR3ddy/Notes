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
