"""Package portable sources using an explicit allowlist, never local secrets."""
from pathlib import Path
import hashlib
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    revision = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, text=True).strip()
    files = [ROOT / name for name in ('README.md', 'DEPLOY.md', 'requirements.txt', 'requirements.runtime.txt', '.gitignore')]
    files += sorted((ROOT / 'Source').rglob('*.py'))
    files += sorted((ROOT / 'sandbox').glob('*.py'))
    files += sorted((ROOT / 'sandbox').glob('*.md'))
    files += sorted((ROOT / 'sandbox').glob('requirements*.txt'))
    files += [ROOT / 'sandbox/tasks.example.json']
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    archive = output / f'yandex-runner-{revision}.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            bundle.write(path, 'Yandex/' + path.relative_to(ROOT).as_posix())
        bundle.writestr('Yandex/BUILD.txt', f'Git revision: {revision}\n')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{digest}  {archive.name}\n')
    print(archive)


if __name__ == '__main__':
    main()
