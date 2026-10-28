import os, time, httpx
from scripts.doctor import traces_read as tr
env = tr.installed_env(os.environ)
host = (env.get("LANGFUSE_HOST") or tr.DEFAULT_HOST).rstrip("/"); auth = (env["LANGFUSE_PUBLIC_KEY"], env["LANGFUSE_SECRET_KEY"])
public = httpx.URL(f"{host}/api/public/projects")
print("route", "ingress" if host == tr.DEFAULT_HOST else "direct")
get = tr.client(env)
s = []
for i in range(15):
    t = time.perf_counter(); get("projects", {}); s.append(time.perf_counter() - t)
print("fresh connection per request: mean %.3f max %.3f" % (sum(s) / len(s), max(s)))
url = public.copy_with(host=tr.INGRESS) if host == tr.DEFAULT_HOST else public
ext = {"sni_hostname": public.host} if host == tr.DEFAULT_HOST else {}
hdr = {"Host": public.host} if host == tr.DEFAULT_HOST else {}
s = []
with httpx.Client() as c:
    for i in range(15):
        t = time.perf_counter(); r = c.get(url, headers=hdr, extensions=ext, auth=auth, timeout=7); r.raise_for_status(); s.append(time.perf_counter() - t)
print("kept connection: first %.3f mean of the other 14 %.3f max %.3f" % (s[0], sum(s[1:]) / 14, max(s[1:])))
