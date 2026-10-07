# Chaiyaphol
Expediment
# requirements.txt

Flask==3.1.2
gunicorn==23.0.0
# run.sh

#!/usr/bin/env bash

set -e

python -m venv .venv

source .venv/bin/activate

python -m pip install --upgrade pip

pip install -r requirements.txt

export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"

python app.py
# project structure

portfolio/
├── app.py
├── requirements.txt
├── run.sh
├── .gitignore
├── templates/
│   ├── index.html
│   └── error.html
└── static/
    └── style.css