import argparse
import getpass
import uuid
from .security import hash_password
from .storage import initialize, db


def create_user(username, company, password):
    if len(password) < 12:
        raise ValueError('Usá una contraseña de al menos 12 caracteres.')
    if not username.strip() or not company.strip():
        raise ValueError('Usuario y empresa obligatorios.')
    initialize()
    with db() as s:
        s.execute('INSERT INTO carbon_accounts VALUES (?,?,?,?)',
                  (str(uuid.uuid4()), username.strip().lower(), company.strip(), hash_password(password)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Crear una cuenta independiente de IVZ Carbon')
    parser.add_argument('username')
    parser.add_argument('company')
    args = parser.parse_args()
    create_user(args.username, args.company, getpass.getpass('Contraseña (mínimo 12 caracteres): '))
    print('Cuenta creada. Elegí inventario vacío o demo al ingresar.')
