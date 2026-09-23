"""Emergency rollback: put every inventory back into the single-body format, with current data.

Run before deploying a version older than the row-based inventory storage:
    python -m backend.unsplit
Records and movements stay in their tables too, so the current version keeps working if it is redeployed.
"""
from .inventory import load
from .storage import db, initialize


def main():
    initialize()
    with db() as s:
        users = [r['account'] for r in s.execute('SELECT account FROM carbon_states').fetchall()]
    for user in users:
        state = load(user)['state']
        with db() as s:
            s.execute('UPDATE carbon_states SET body=? WHERE account=?', (s.json(state), user))
    print(f'{len(users)} inventarios vueltos al formato de un solo bloque.')


if __name__ == '__main__':
    main()
