# IVZ Carbon · publicación

- Sitio: https://ivzcarbon.vercel.app
- Repositorio: https://github.com/iandesmarchelier/IVZCarbon
- Proyecto Vercel: `ivzcarbon`, equipo `ian19-09a7`.
- Este proyecto es independiente de IVZ Sustainability Hub.
- Publicar frontend y backend juntos y verificar login y persistencia en la URL pública.

## Configuración

`api/index.py` expone FastAPI; `vercel.json` dirige las rutas a esa función. La raíz del proyecto es la raíz del repositorio IVZCarbon, con framework Other, sin comando de build ni directorio de salida personalizado.

Configurar PostgreSQL alojado mediante `CARBON_DATABASE_URL` o la variable `DATABASE_URL` de la integración Vercel del propio proyecto. Nunca usar una URL localhost en Vercel. `CARBON_ENV=production` y las cookies Secure se activan también por la variable VERCEL de la plataforma. SQLite está bloqueado en Vercel.

Para crear inicialmente el usuario `demo`, configurar `CARBON_BOOTSTRAP_PASSWORD_HASH` con su hash scrypt, generado mediante `backend.security.hash_password`. La contraseña no debe aparecer en el repositorio. El arranque crea la cuenta solamente si no existe; después puede retirarse esta variable sin perder la cuenta. El inventario se inicializa al ingresar o se importa mediante la API autenticada.

El guardado usa revisiones para evitar sobrescrituras. El límite del inventario enviado por solicitud en Vercel es 4 MB (por debajo del [límite de la plataforma](https://vercel.com/docs/functions/limitations)). Para inventarios mayores se requiere carga incremental.

Excluir de publicación `data/`, `.env`, bases locales, credenciales, archivos dump y el runtime portátil de PostgreSQL. El esquema se crea automáticamente en la base remota al arrancar.

## Verificación

1. Confirmar despliegue Ready en Vercel.
2. Verificar `/health`: `status=ok`, `database=postgresql`.
3. Ingresar con la cuenta demo y comprobar que `/api/state` esté protegido sin sesión.
4. Guardar un cambio y recargar; comprobar persistencia.
5. Confirmar que no aparece la barra inferior de guardado.
