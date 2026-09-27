"""Run the same audited calculation on a GitHub-hosted runner."""
from pathlib import Path
import shutil
import subprocess
import sys
import time
from update import SKILL, load_config, default_end
from export_share import export


def refresh_with_retries(end, delays):
    """Retry complete audited retrievals; never publish a partial failed attempt."""
    for attempt in range(len(delays) + 1):
        try:
            subprocess.run([sys.executable, str(SKILL/'scripts/update.py'), 'refresh', '--end', end], check=True)
            subprocess.run([sys.executable, str(SKILL/'scripts/regions.py'), '--end', end], check=True)
            return
        except subprocess.CalledProcessError:
            if attempt == len(delays):
                raise
            print(f'Audited refresh attempt {attempt+1} failed; retrying in {delays[attempt]} seconds', flush=True)
            time.sleep(delays[attempt])


def main():
    config, _, _ = load_config()
    output = Path(config['output_dir'])
    (output / 'raw').mkdir(parents=True, exist_ok=True)
    for receipt in (SKILL / 'seed-raw').glob('*.json'):
        shutil.copy2(receipt, output / 'raw' / receipt.name)
    # Missing new secondary-source receipts fail closed; never relabel stale data as new.
    end=default_end()
    refresh_with_retries(end, config['publication']['refresh_retry_delays_seconds'])
    export(Path('.site/index.html'))
    for name in ['index.html', 'publication.json']:
        shutil.copy2(Path('.site')/name, name)


if __name__ == '__main__':
    main()
