"""Build an allowlisted, secret-free disposable Vercel demonstration package."""
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / ".tools" / "vercel-demo"
DEST.mkdir(parents=True, exist_ok=True)
for name in ("app.py", "legado.py", "jev.py", "haiku.py", "rastro.py", "requirements.txt"):
    target = "produto.py" if name == "app.py" else name
    shutil.copy2(ROOT / name, DEST / target)
for name in ("templates", "static"):
    shutil.copytree(ROOT / name, DEST / name, dirs_exist_ok=True)

# Only the generated package changes; the local application's storage is untouched.
p = DEST / "produto.py"
source = p.read_text(encoding="utf-8")
source = source.replace('load_dotenv(BASE / ".env")', '# Demo: no local .env is loaded.')
source = source.replace('UPLOADS = BASE / "uploads"', 'UPLOADS = Path(os.environ["DEMO_DATA_DIR"]) / "uploads"')
source = source.replace('VAPID_PEM = BASE / "vapid_private.pem"', 'VAPID_PEM = Path(os.environ["DEMO_DATA_DIR"]) / "vapid_private.pem"')
p.write_text(source, encoding="utf-8")
p = DEST / "legado.py"
p.write_text(p.read_text(encoding="utf-8").replace('load_dotenv(BASE / ".env")', '# Demo: no local .env is loaded.'), encoding="utf-8")
for p in (DEST / "templates" / "df").glob("*.html"):
    source = p.read_text(encoding="utf-8").replace("http://localhost:8001", "/legado/")
    source = source.replace(">localhost:8001<", ">Abrir legado simulado<")
    p.write_text(source, encoding="utf-8")
shutil.copy2(ROOT / "tools" / "vercel_demo.py", DEST / "main.py")
shutil.copy2(ROOT / "tools" / "demo_store.py", DEST / "demo_store.py")
dependencies = [line.strip() for line in (ROOT / "requirements.txt").read_text().splitlines()
                if line.strip() and not line.startswith("#")]
(DEST / "pyproject.toml").write_text('[project]\nname = "dataforge-demo"\nversion = "0.1.0"\nrequires-python = ">=3.12"\ndependencies = ' + json.dumps(dependencies) + '\n\n[tool.vercel]\nentrypoint = "main:app"\n\n[tool.vercel.fastapi.static]\ncdn = false\n', encoding="utf-8")
(DEST / "vercel.json").write_text(json.dumps({"$schema": "https://openapi.vercel.sh/vercel.json", "framework": "fastapi", "functions": {"main.py": {"maxDuration": 60}}}, indent=2), encoding="utf-8")
(DEST / ".vercelignore").write_text(".env*\n*.db*\n*.pem\n__pycache__/\nuploads/\n.git/\n", encoding="utf-8")
print(f"Prepared demo: {DEST}")
