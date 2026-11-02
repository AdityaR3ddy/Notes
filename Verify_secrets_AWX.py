# Proves migrated secrets on AWX equal Tower's, without showing them: compares HMAC fingerprints
# made on Tower (fingerprints_*.json) with ones computed here. Prints names and OK/MISMATCH only.
# Run inside the AWX task pod (the VERIFY KEY was printed once by the migration run):
#   env FINGERPRINTS=/tmp/fingerprints.json VERIFY_KEY=<key> awx-manage shell < awx_verify_secrets.py
import hashlib, hmac, json, os
from awx.main.models import Credential
key = os.environ["VERIFY_KEY"].encode()
total = problems = 0
for e in json.load(open(os.environ["FINGERPRINTS"])):
    wrong = []
    try:
        c = Credential.objects.get(pk=e["id"])
        for f, want in e["fields"].items():
            got = hmac.new(key, (f + ":" + str(c.get_input(f))).encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(got, want):
                wrong.append(f)
    except Exception as ex:
        wrong.append(type(ex).__name__)
    total += 1
    problems += 1 if wrong else 0
    print("MISMATCH" if wrong else "OK", e["name"], ",".join(wrong))
print("DONE: %d credentials checked, %d with problems" % (total, problems))
