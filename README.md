# IVZ Carbon · backend Python y PostgreSQL

Aplicación independiente basada en `IVZCarbon V3.5 - Fix bugs.html`. El HTML original permanece en su ubicación; la copia conectada al backend está en `static/index.html`. El backend del Sustainability Hub no se modifica.

## PostgreSQL con Docker

Desde esta carpeta, con Docker Desktop instalado y activo:

```powershell
Copy-Item .env.example .env
# Completar CARBON_DB_PASSWORD en .env con una contraseña aleatoria hexadecimal.
docker compose up -d --build
docker compose exec app python -m backend.manage administrador "Mi empresa"
```

El último comando solicita una contraseña de al menos 12 caracteres. Abrir http://127.0.0.1:8001 y elegir inventario vacío o datos demo. PostgreSQL 17 usa un volumen persistente y no expone su puerto al exterior. La app crea el esquema versión 1 automáticamente bajo un bloqueo de migración.

## PostgreSQL existente, sin Docker

Crear previamente una base dedicada y un usuario con permisos sobre ella. Python 3.12 o posterior:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:CARBON_DATABASE_URL = 'postgresql://usuario:password@servidor:5432/carbon?sslmode=require'
python -m backend.manage administrador "Mi empresa"
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8001
```

Las variables de `.env` las carga Docker Compose. Para Python directo, configurarlas en el entorno. `CARBON_ENV=production` exige PostgreSQL y activa cookies Secure: servir detrás de HTTPS. Nunca usar el secreto ni la base del Hub por defecto: Carbon utiliza `CARBON_DATABASE_URL` y tablas `carbon_*`.

## Aplicación local preparada con PostgreSQL

En esta computadora, ejecutar `iniciar-postgres.ps1`. Usa el Python y las dependencias disponibles del workspace, inicia PostgreSQL si hace falta y sirve la aplicación en http://127.0.0.1:8001. La cuenta está en `data/acceso-local.txt`.

La base `ivz_carbon` escucha únicamente en `127.0.0.1:55432`, con autenticación SCRAM y un usuario de aplicación sin privilegios de superusuario. El runtime portable PostgreSQL 17.11 proviene de [EDB](https://www.enterprisedb.com/download-postgresql-binaries). No se instaló un servicio de Windows ni se modificó PATH. Runtime, base y credenciales están bajo `data/`, excluido de Git. `data/postgres-local.json` contiene la conexión y la cuenta administrativa local; no compartirlo.

Para detener la base después de cerrar el servidor: `& '.\data\runtime\pgsql\bin\pg_ctl.exe' -D '.\data\pgdata' -m fast -w stop`. No iniciar un segundo servidor web si el puerto 8001 ya está ocupado.

Se conserva `iniciar-local.ps1` como alternativa explícita SQLite de pruebas. Sin `CARBON_DATABASE_URL` ni `CARBON_SQLITE_PATH`, el backend rechaza el arranque.

## Funcionalidad

- Login con contraseña scrypt, cookie HttpOnly, expiración de 8 horas y límite de intentos.
- Una cuenta independiente por empresa; cada consulta filtra por la cuenta autenticada.
- Elección inicial entre inventario vacío y demo. Guardado automático silencioso cada 3 segundos y aviso al salir con cambios pendientes. Sin barra inferior de guardado; los errores muestran un aviso con opción de descargar un respaldo.
- Persistencia de sitios, procesos, líneas, equipos, actividades, factores, residuos, registros, movimientos, perfiles de importación e incertidumbre.
- Revisión optimista: una pestaña desactualizada recibe 409 y no pisa cambios de otra. Ante conflicto o sesión vencida se conserva la copia en memoria y se permite descargarla.
- Motor Python recalcula las emisiones a partir de cantidades y factores; no confía en los totales enviados por JavaScript. Incluye alcances 1/2/3, categorías, CO2 biogénico separado, location-based e incertidumbre equivalente al prototipo.
- Auditoría de accesos y guardados; exportación JSON y CSV mediante API. `/api/summary` entrega el cálculo consolidado del servidor.
- Se conserva la navegación y los importadores de la V3.5. El navegador mantiene su motor para interacción inmediata; Python valida y recalcula al guardar.

## Modelo de datos

`carbon_accounts` y `carbon_sessions`: cuentas y sesiones con tokens hasheados. `carbon_states`: snapshot completo JSONB y revisión para reconstruir la interfaz sin pérdidas. `carbon_entities`: catálogos y movimientos por cuenta/tipo/ID en JSONB. `carbon_records`: registros consultables por período, sitio y alcance, cantidad y emisiones calculadas, más detalle JSONB. `carbon_events`: auditoría del servidor. `carbon_login_limits`: limitación de intentos. `carbon_schema`: versión de migración.

Snapshot, registros consultables y auditoría se guardan en una única transacción. El mecanismo sincroniza el inventario completo, con límite de 32 MB y 50.000 elementos por colección; para volúmenes mayores hay que evolucionar a operaciones incrementales. La auditoría registra operaciones y totales, no versiones históricas completas del dataset.

## API

Documentación interactiva: `/docs`. Las operaciones de escritura requieren `X-IVZ-Carbon: 1` y las de inventario requieren la cookie de sesión.

| Ruta | Función |
|---|---|
| `POST /api/login`, `POST /api/logout`, `GET /api/me` | Acceso |
| `POST /api/initialize` | Inicializar una vez, `{ "mode": "empty" }` o `demo` |
| `GET /api/state`, `PUT /api/state` | Leer/guardar `{revision, state}` |
| `GET /api/summary?year=2025&site=S1` | Indicadores calculados en Python |
| `GET /api/records?year=2025&scope=1&limit=100&offset=0` | Registros paginados |
| `GET /api/export`, `GET /api/inventory.csv` | Respaldo e inventario |
| `GET /api/audit`, `GET /health` | Auditoría y salud de la base |

Para restaurar un respaldo mediante API: obtener la revisión actual de `/api/state`, enviar esa revisión junto con `state` del respaldo mediante `PUT /api/state`. Descargar antes la versión actual. Un conflicto 409 requiere revisar qué versión conservar; no reintentar automáticamente con la revisión nueva.

## Pruebas y alcance verificado

Desde la carpeta padre del proyecto Carbon:

```powershell
python -m unittest discover -s carbon/tests -v
node --check carbon/static/bridge.js
```

La prueba de equivalencia compara 2.658 registros contra resultados extraídos del motor JavaScript V3.5. La suite cubre sesiones, aislamiento, conflictos, validación, recálculo, inicialización y exportación. Las pruebas locales usan SQLite. Para comprobar una instancia PostgreSQL dedicada, desde esta carpeta y con `CARBON_DATABASE_URL` configurada: `python -m backend.check_postgres`.

Verificado en esta computadora: siete pruebas automáticas, prueba real contra PostgreSQL 17.11 (JSONB, persistencia, consulta de registros y conflicto entre revisiones), ingreso y creación de una planta desde el navegador, recuperación al recargar y navegación por clasificación, incertidumbre, movimientos y factores sin errores JavaScript. No se publicó esta aplicación en Internet.

## Límites heredados del prototipo

Los conectores SAP/ERP y las lecturas de factura simuladas siguen identificados como simulaciones; este backend no convierte esos controles en integraciones externas reales. Los factores y sus fuentes se conservan como referencias de la V3.5, sin validación independiente ni certificación. El backend no incorpora OCR, gestión de evidencias binarias ni permisos por rol entre varios usuarios de una misma empresa.

Antes de utilizar el sistema para reportes oficiales, validar el catálogo y los criterios de incertidumbre. El cálculo actual reproduce la hipótesis de independencia entre registros del prototipo.

Para respaldar PostgreSQL: `docker compose exec -T db pg_dump -U carbon -d carbon -Fc -f /tmp/carbon.dump`, luego `docker compose cp db:/tmp/carbon.dump ./carbon.dump`. Conservar fuera del repositorio y probar la restauración en otra base. No ejecutar `docker compose down -v`: elimina el volumen.
