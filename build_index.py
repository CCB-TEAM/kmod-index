#!/usr/bin/env python3
"""Builds index.json for the KMod Launcher.

    python build_index.py                     # crawl GitHub repositories with the topic "kmod-mod"
    python build_index.py --local packages/   # index the .kmod files of a folder (testing, private indexes)

Standard library only. GITHUB_TOKEN (set by GitHub Actions) raises the API limits.
Output: index.json, icons/<id>.<ext>, covers/<id>.<ext> inside --out (default: the current folder).
Packages are only read as zip archives; nothing in them is ever executed.
"""
import argparse, datetime, hashlib, io, json, os, re, shutil, sys, urllib.parse, urllib.request, zipfile

TOPIC = "kmod-mod"
MAX_PACKAGE = 100 * 1024 * 1024
MAX_IMAGE = 512 * 1024
KEEP_VERSIONS = 5
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
ID_RE = re.compile(r"^[a-z0-9_]+$")


def log(*a):
    print(*a, file=sys.stderr)


def get(url, accept="application/vnd.github+json", limit=MAX_PACKAGE):
    headers = {"User-Agent": "kmod-index", "Accept": accept}
    token = os.environ.get("GITHUB_TOKEN")
    if token and urllib.parse.urlsplit(url).hostname == "api.github.com":
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("too large")
    return data


def api(path):
    return json.loads(get("https://api.github.com/" + path, limit=20 * 1024 * 1024))


def plain_path(name):
    parts = name.replace("\\", "/").split("/")
    return bool(name) and not name.startswith("/") and ":" not in name and ".." not in parts


def read_package(data):
    """-> (manifest, {"icon": (ext, bytes), "cover": ...}) or raises ValueError."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        manifest = json.loads(z.read("mod.json").decode("utf-8-sig"))
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"not a .kmod package: {e}")
    if not isinstance(manifest, dict) or not ID_RE.match(str(manifest.get("id", ""))) or not manifest.get("version"):
        raise ValueError("mod.json needs a valid id and a version")
    images = {}
    for kind in ("icon", "cover"):
        name = str(manifest.get(kind) or kind + ".png")
        ext = os.path.splitext(name)[1].lower()
        if not plain_path(name) or ext not in IMAGE_EXT:
            continue
        try:
            if z.getinfo(name).file_size <= MAX_IMAGE:
                images[kind] = (ext, z.read(name))
        except KeyError:
            pass
    return manifest, images


def platforms_of(m):
    p = m.get("platform")
    if not p:
        return ["Android", "Windows"]
    p = [p] if isinstance(p, str) else p
    return [x for x in ("Android", "Windows") if x.lower() in [str(v).lower() for v in p]]


def version_entry(m, data, url, published):
    return {
        "version": str(m["version"]),
        "url": url,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "published": published,
        "platforms": platforms_of(m),
        "native": bool(m.get("native")),
        "permissions": [str(x) for x in m.get("permissions", [])],
        "depends": [str(x) for x in m.get("depends", [])],
    }


def mod_entry(m, images, out, versions, extra):
    e = {
        "id": m["id"],
        "name": str(m.get("name") or m["id"]),
        "author": str(m.get("author") or ""),
        "description": str(m.get("description") or ""),
        "versions": versions,
    }
    for kind, folder in (("icon", "icons"), ("cover", "covers")):
        if kind in images:
            ext, blob = images[kind]
            os.makedirs(os.path.join(out, folder), exist_ok=True)
            with open(os.path.join(out, folder, m["id"] + ext), "wb") as f:
                f.write(blob)
            e[kind] = f"{folder}/{m['id']}{ext}"
    e.update(extra)
    return e


def from_folder(folder, out):
    found = {}
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(".kmod"):
            continue
        with open(os.path.join(folder, name), "rb") as f:
            data = f.read()
        try:
            m, images = read_package(data)
        except ValueError as e:
            log(name, e)
            continue
        os.makedirs(os.path.join(out, "packages"), exist_ok=True)
        target = os.path.join(out, "packages", name)
        if os.path.abspath(target) != os.path.abspath(os.path.join(folder, name)):
            shutil.copyfile(os.path.join(folder, name), target)
        today = datetime.date.today().isoformat()
        v = version_entry(m, data, "packages/" + urllib.parse.quote(name), today)
        found[m["id"]] = mod_entry(m, images, out, [v], {"repo": "", "stars": 0, "updated": today})
    return list(found.values())


def from_github(out, old):
    known = {(v["url"]): v for m in old.get("mods", []) for v in m.get("versions", [])}
    repos, page = [], 1
    while page <= 10:
        r = api(f"search/repositories?q=topic:{TOPIC}&per_page=100&page={page}")
        repos += r.get("items", [])
        if len(r.get("items", [])) < 100:
            break
        page += 1
    repos.sort(key=lambda r: r["created_at"])  # the older repository keeps a contested id
    mods, taken = [], set()
    for repo in repos:
        full = repo["full_name"]
        try:
            releases = api(f"repos/{full}/releases?per_page=20")
            versions, latest = [], None
            for rel in releases:
                if rel.get("draft") or len(versions) >= KEEP_VERSIONS:
                    continue
                asset = next((a for a in rel.get("assets", []) if a["name"].lower().endswith(".kmod") and a["size"] <= MAX_PACKAGE), None)
                if not asset:
                    continue
                url = asset["browser_download_url"]
                data = get(url, accept="application/octet-stream")
                m, images = read_package(data)
                if latest and m["id"] != latest[0]["id"]:
                    continue
                before = known.get(url)
                if before and before["sha256"] != hashlib.sha256(data).hexdigest():
                    log(full, "the file of", url, "changed after it was indexed")
                versions.append(version_entry(m, data, url, rel.get("published_at", "")[:10]))
                latest = latest or (m, images)
            if not latest:
                continue
            m, images = latest
            if m["id"] in taken:
                log(full, "id", m["id"], "is already taken, skipped")
                continue
            taken.add(m["id"])
            mods.append(mod_entry(m, images, out, versions, {"repo": repo["html_url"], "stars": repo["stargazers_count"], "updated": versions[0]["published"]}))
        except Exception as e:  # one broken repository must not stop the index
            log(full, e)
    return mods


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", help="folder with .kmod files instead of GitHub")
    ap.add_argument("--out", default=".")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    target = os.path.join(args.out, "index.json")
    old = load_json(target, {})
    mods = from_folder(args.local, args.out) if args.local else from_github(args.out, old)
    blocked = load_json("blocklist.json", {})  # {"ids": [], "repos": ["owner/name"]}
    verified = load_json("verified.json", [])  # ["owner/name", or a mod id for local indexes]
    kept = []
    for m in mods:
        full = m.get("repo", "").replace("https://github.com/", "")
        if m["id"] in blocked.get("ids", []) or (full and full in blocked.get("repos", [])):
            continue
        m["verified"] = (full or m["id"]) in verified
        kept.append(m)
    kept.sort(key=lambda m: m["id"])
    if old.get("mods") == kept:
        log("no changes")
        return
    index = {"schema": 1, "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "blocked": blocked.get("ids", []), "mods": kept}
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
        f.write("\n")
    log(f"{len(kept)} mod(s) -> {target}")


if __name__ == "__main__":
    main()
