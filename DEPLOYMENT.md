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

El guardado usa revisiones para evitar sobrescrituras. Vercel limita cada solicitud a 4,5 MB ([límite de la plataforma](https://vercel.com/docs/functions/limitations)), así que el inventario no viaja en un solo bloque: el catálogo (factores, sitios, períodos, configuración) queda en `carbon_states`, y los registros y movimientos son filas en `carbon_records` y `carbon_entities` (`kind='MOV'`), con su orden en `seq`. La pantalla los carga por páginas (`/api/state/catalogue`, `/api/state/rows`) y guarda solo lo que cambió (`/api/state/changes`); un cambio grande viaja en partes (`/api/state/upload`) y se aplica entero o nada. El servidor valida y calcula siempre el inventario completo. Las descargas completas (`/api/export`, `/api/inventory.csv`) salen en streaming, que Vercel no limita a 4,5 MB, leyendo la base por tandas.

Cada cuenta en el formato anterior (todo en `carbon_states`) se convierte sola la primera vez que se lee, y el bloque original queda en `carbon_state_backups`. Para volver a una versión anterior a este formato, correr antes `python -m backend.unsplit` con la misma base: rearma el bloque único con los datos al día.

Excluir de publicación `data/`, `.env`, bases locales, credenciales, archivos dump y el runtime portátil de PostgreSQL. El esquema se crea automáticamente en la base remota al arrancar.

## Contenedor (Azure, SAP BTP u otro hosting con Docker)

El `Dockerfile` arma la misma aplicación con Tesseract y el idioma español, que Vercel no puede instalar: ahí las facturas y manifiestos en foto o PDF escaneado se leen con OCR en vez de responder `ocr-unavailable`. Vercel ignora el Dockerfile, así que ambos despliegues conviven.

- Variables: `CARBON_DATABASE_URL` (obligatoria; fuera de Vercel no se lee `DATABASE_URL`), `CARBON_BOOTSTRAP_PASSWORD_HASH` y `CARBON_ADMIN_PASSWORD_HASH` si corresponden. La imagen ya trae `CARBON_ENV=production`: cookies Secure y SQLite bloqueado.
- Puerto: 8001, o el que indique la plataforma en `PORT` (Cloud Foundry, App Service).
- La imagen no guarda nada en disco: datos y documentos van a PostgreSQL, así que se puede reiniciar o escalar sin perder información.
- Fuera de Vercel el límite por guardado sube de 4 MB a 32 MB; el guardado por partes sigue funcionando igual.
- Prueba local con Docker Desktop: copiar `.env.example` a `.env`, completar `CARBON_DB_PASSWORD` y correr `docker compose up --build` (http://localhost:8001).
- `.github/workflows/contenedor.yml` arma la imagen en cada push, corre todas las pruebas dentro de ella (incluida la lectura OCR de una factura) y comprueba `/health`.

## Verificación

1. Confirmar despliegue Ready en Vercel.
2. Verificar `/health`: `status=ok`, `database=postgresql`. En el contenedor, además `ocr=tesseract-spa` (en Vercel es `off`).
3. Ingresar con la cuenta demo y comprobar que `/api/state` esté protegido sin sesión.
4. Guardar un cambio y recargar; comprobar persistencia.
5. Confirmar que no aparece la barra inferior de guardado.
