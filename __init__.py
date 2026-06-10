from flask import Blueprint

# Create the Blueprint for the PAM module.
pam_bp = Blueprint('pam', __name__, template_folder='templates')

from . import routes