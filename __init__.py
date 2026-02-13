# myproject/app/pam/__init__.py

from flask import Blueprint

# Створюємо Blueprint для нашого дашборду.
pam_bp = Blueprint('pam', __name__, template_folder='templates')

from . import routes