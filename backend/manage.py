import argparse
import getpass
import uuid
from datetime import datetime, timezone
from .mfa import clean_email
from .security import hash_password
from .storage import initialize, db


def create_user(username, company, password, email):
    if len(password) < 12:
        raise ValueError('Usá una contraseña de al menos 12 caracteres.')
    if not username.strip() or not company.strip():
        raise ValueError('Usuario y empresa obligatorios.')
    email = clean_email(email)
    initialize()
    with db() as s:
        s.execute('INSERT INTO carbon_accounts (id,username,company,password,role,active,created,email) VALUES (?,?,?,?,?,?,?,?)',
                  (str(uuid.uuid4()), username.strip().lower(), company.strip(), hash_password(password),
                   'client', True, datetime.now(timezone.utc).isoformat(), email))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Crear una cuenta independiente de IVZ Carbon')
    parser.add_argument('username')
    parser.add_argument('company')
    parser.add_argument('email', help='correo donde llega el código de acceso')
    args = parser.parse_args()
    create_user(args.username, args.company, getpass.getpass('Contraseña (mínimo 12 caracteres): '), args.email)
    print('Cuenta creada. Elegí inventario vacío o demo al ingresar.')
