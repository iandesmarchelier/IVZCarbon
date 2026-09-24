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

- Reportes anuales ISO 14064-1: en Reportes, seleccionar año y sitios y pulsar «Generar reporte anual». Completar el contexto metodológico (se reutilizan los textos del último informe), generar una versión calculada por el servidor y abrir su vista con portada, gráficos SVG, tablas y formato A4. «Imprimir / Guardar PDF» utiliza el diálogo del navegador. El historial conserva resultados y textos por empresa. La aprobación interna exige resolver datos demo, asignaciones pendientes y campos descriptivos; no acredita conformidad ni verificación externa. El informe identifica brechas, presenta energía por ubicación y los factores seleccionados sin atribuir automáticamente cumplimiento market-based, y propone una clasificación ISO que requiere revisión metodológica.

  Los textos se reutilizan únicamente dentro del mismo perímetro; los borradores de la pantalla se separan por año y sitio. El historial muestra la aprobación interna. Los meses sin registros se identifican como «Sin datos» también en los gráficos. Para años cerrados se conserva la incertidumbre consolidada del cierre; la incertidumbre histórica por sitio se declara no disponible porque los cierres existentes no guardan ese detalle. Los campos que solo contienen espacios se consideran pendientes.

- Login con contraseña scrypt, cookie HttpOnly, expiración de 8 horas y límite de intentos.
- Una cuenta independiente por empresa; cada consulta filtra por la cuenta autenticada.
- Elección inicial entre inventario vacío y demo. Guardado automático silencioso cada 3 segundos y aviso al salir con cambios pendientes. Sin barra inferior de guardado; los errores muestran un aviso con opción de descargar un respaldo.
- Persistencia de sitios, procesos, líneas, equipos, actividades, factores, residuos, registros, movimientos, perfiles de importación e incertidumbre.
- Revisión optimista: una pestaña desactualizada recibe 409 y no pisa cambios de otra. Ante conflicto o sesión vencida se conserva la copia en memoria y se permite descargarla.
- Motor Python recalcula las emisiones a partir de cantidades y factores; no confía en los totales enviados por JavaScript. Incluye alcances 1/2/3, categorías, CO2 biogénico separado, location-based e incertidumbre equivalente al prototipo.
- Auditoría de accesos y guardados; exportación JSON y CSV mediante API. `/api/summary` entrega el cálculo consolidado del servidor.
- Se conserva la navegación y los importadores de la V3.5. El navegador mantiene su motor para interacción inmediata; Python valida y recalcula al guardar.
- Movilidad (reemplaza al mapa de movimientos de América): globo 3D del planeta con los traslados de insumos por tramo y los viajes de negocio. Cada trayecto va de verde (menor emisión) a rojo (mayor) y los de mayor emisión laten; el mapa de calor muestra las ubicaciones con más emisiones de movilidad (cada trayecto reparte su emisión entre origen y destino) o, a elección, el inventario total por sitio. Los viajes se ubican con `route` (origen y destino de la planilla de viajes, con coordenadas opcionales) o leyendo el concepto («Vuelo — Buenos Aires → Houston»). El globo (`static/globe.js`, `static/world.js` con contornos Natural Earth de dominio público) es un canvas propio, sin teselas ni servicios de mapas; Leaflet ya no se usa.
- Asignación automática de factores (`backend/matching.py`): cada dato que entra sin factor (posición de OC o baja de stock, viaje, residuo, traslado, transporte en t·km, consumo de energía o combustible, otra actividad; por Excel o carga manual) recibe el factor de la biblioteca más parecido a su descripción. La similitud la calcula PostgreSQL con `pg_trgm` entre los factores del mismo alcance, categoría y unidad (un factor declarado por un proveedor solo compite en las compras de ese proveedor): puntaje = mejor término del factor de (`similarity` + mayor `strict_word_similarity` en ambos sentidos) / 2. Cada factor se busca por su título, los términos de la biblioteca IVZ (`LIBRARY_TERMS`: «vuelo» → Avión, «hotelería» → Hotel…) y sus términos propios (`alias`, editables en Factores de emisión). El registro guarda `fm` (texto buscado, porcentaje, candidatos, empate, aprobación). Toda asignación automática queda pendiente hasta que una persona la aprueba en «Asignación de factores», que las ordena de menor a mayor similitud; el porcentaje aparece al lado del factor en las tablas de registros, OC y transacciones. Dos factores con el mismo porcentaje son un empate: se advierte y no se aprueba en lote. Al aprobar, el texto pasa a ser término del factor y la próxima vez coincide al 100%. Un año con asignaciones sin aprobar no se puede cerrar. «Analizar registros existentes» aplica lo mismo a compras, viajes, residuos y traslados ya cargados (años abiertos). Si la base no tiene `pg_trgm`, el servidor calcula lo mismo en Python con resultados idénticos; `/health` informa el motor en `similarity`. Las facturas de energía, los manifiestos de residuos y los movimientos por tramo conservan su factor determinado por sitio, tratamiento o modo.
- Carga masiva de facturas y manifiestos («Ubicaciones y facturas» → «Carga masiva», o «Carga por Excel» → «Varios documentos a la vez»): se suben juntos los PDF o fotos de facturas de electricidad y gas y de manifiestos de residuos. `POST /api/parse-document` con `kind=auto` lee cada uno (texto del PDF; OCR solo donde Tesseract está instalado, no en Vercel), dice si es electricidad, gas o manifiesto, y devuelve período, cantidad, proveedor, documento y pistas de ubicación (N° de cliente/cuenta/suministro/NIS/medidor y domicilio). La pantalla asigna la sede por un número ya aprendido para esa sede (`SITES[].supply`), o por su nombre y dirección en el texto, y el residuo por su nombre. Todo queda en una tabla para revisar; al guardar, dos facturas de la misma sede, energía y período se suman, cada factura reemplaza lo cargado de esa energía en ese período (como la carga de a una), y los números de cliente que aparecieron en una sola sede quedan aprendidos (se ven y se pueden quitar en la tarjeta de la sede).
- Configuración por cliente (`backend/features.py`): en Administración, «Configurar» enciende o apaga para cada cuenta las secciones del menú y los conectores de Integraciones. Lo apagado desaparece del menú y del recorrido guiado, y el servidor lo bloquea (reportes, tokens y lectura del Hub con token devuelven 403); los datos no se borran. «Consumo energético por equipo» viene apagada por defecto. El administrador también genera y revoca los tokens de IVZ Sustainability Hub de la cuenta.

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
| `GET /api/state/catalogue`, `GET /api/state/rows`, `POST /api/state/changes`, `POST /api/state/upload` | Carga por páginas y guardado de cambios (lo que usa la pantalla) |
| `GET /api/summary?year=2025&site=S1` | Indicadores calculados en Python; un año cerrado devuelve sus resultados congelados y `closed` |
| `GET /api/closures`, `POST /api/closures` `{year}`, `POST /api/closures/{year}/reopen` `{reason}` | Cierre de años: cerrar (cliente o administrador), reabrir (solo administrador entrando como el cliente) |
| `GET /api/records?year=2025&scope=1&limit=100&offset=0` | Registros paginados |
| `GET /api/export`, `GET /api/inventory.csv` | Respaldo e inventario |
| `POST /api/factors/match` `{items:[{text, scope?, cat?, unit?, supplier?}]}`, `GET /api/factors/terms` | Factor de la biblioteca más parecido a cada descripción (hasta 5.000 por pedido; acepta también token de integración) y términos de búsqueda de la biblioteca |
| `GET /api/audit`, `GET /health` | Auditoría y salud de la base |
| `POST /api/parse-document` (form: `kind` = `elec`, `gas`, `waste` o `auto`, `file`) | Lectura de una factura o manifiesto; `auto` detecta el tipo y devuelve pistas de ubicación. Guarda el archivo (`carbon_documents`, solo PDF/PNG/JPEG por sus primeros bytes) y devuelve su id en `document` |
| `GET /api/documents/{id}` | Abre el documento subido («Ver documento» en la trazabilidad del registro, que lo guarda en `origin.files`). Un archivo que ningún registro guardado nombra se borra al día siguiente |
| `GET`/`PUT /api/admin/accounts/{id}/settings`, `POST`/`DELETE /api/admin/accounts/{id}/tokens` | Administrador: secciones e integraciones de la cuenta, tokens del Hub |

Para restaurar un respaldo mediante API: obtener la revisión actual de `/api/state`, enviar esa revisión junto con `state` del respaldo mediante `PUT /api/state`. Descargar antes la versión actual. Un conflicto 409 requiere revisar qué versión conservar; no reintentar automáticamente con la revisión nueva. Un 423 indica que el respaldo cambia registros de un año cerrado.

## Aislamiento entre clientes

Aislamiento entre clientes: además del filtro por cuenta de cada consulta, PostgreSQL lo hace cumplir con row-level security. Cada conexión nombra su cuenta (`db(cuenta)`; `db(SYSTEM)` queda para el ingreso, el administrador y los scripts). La de un cliente trabaja como el rol `ivz_carbon_tenant`, que solo ve y escribe filas de esa cuenta en toda tabla con columna `account` (y su propia fila en `carbon_accounts`), aunque una consulta olvide el `WHERE account=?`. Al arrancar, la aplicación crea ese rol y las políticas que falten, así que el usuario de la base necesita permiso para crear roles (el dueño de Neon lo tiene). Si no puede, la aplicación sigue funcionando sin esa protección y lo registra en el log como «Row-level security is NOT enforced».

## Pruebas y alcance verificado

Desde la carpeta padre del proyecto Carbon:

```powershell
python -m unittest discover -s carbon/tests -v
# Opcional: compara el motor Python con pg_trgm en una base PostgreSQL de prueba
$env:CARBON_TEST_POSTGRES_URL = 'postgresql://usuario:password@127.0.0.1:55432/base_de_prueba'
node --check carbon/static/bridge.js
```

Para probar el aislamiento en PostgreSQL, desde la raíz del repositorio: `python -m unittest discover -s tests` con `CARBON_TEST_ISOLATION_URL` apuntando a una base descartable con «test» en el nombre (las pruebas borran sus tablas). `tests/test_isolation.py` repite ahí las pruebas de la API con row-level security y comprueba que una cuenta no lee ni escribe filas de otra. Sin esa variable, igual verifica que `db(SYSTEM)` solo aparezca en el ingreso, el administrador y los scripts.

La prueba de equivalencia compara 2.658 registros contra resultados extraídos del motor JavaScript V3.5. La suite cubre sesiones, aislamiento, conflictos, validación, recálculo, inicialización y exportación. Las pruebas locales usan SQLite. Para comprobar una instancia PostgreSQL dedicada, desde esta carpeta y con `CARBON_DATABASE_URL` configurada: `python -m backend.check_postgres`.

Verificado en esta computadora: siete pruebas automáticas, prueba real contra PostgreSQL 17.11 (JSONB, persistencia, consulta de registros y conflicto entre revisiones), ingreso y creación de una planta desde el navegador, recuperación al recargar y navegación por clasificación, incertidumbre, movimientos y factores sin errores JavaScript. No se publicó esta aplicación en Internet.

## Límites heredados del prototipo

Los conectores SAP/ERP siguen identificados como simulaciones, igual que las facturas y manifiestos «de ejemplo» (la lectura de un documento subido es real: texto del PDF, u OCR donde está instalado); este backend no convierte esos controles en integraciones externas reales. Los factores y sus fuentes se conservan como referencias de la V3.5, sin validación independiente ni certificación. El backend no incorpora OCR, gestión de evidencias binarias ni permisos por rol entre varios usuarios de una misma empresa.

Antes de utilizar el sistema para reportes oficiales, validar el catálogo y los criterios de incertidumbre. El cálculo actual reproduce la hipótesis de independencia entre registros del prototipo.

Para respaldar PostgreSQL: `docker compose exec -T db pg_dump -U carbon -d carbon -Fc -f /tmp/carbon.dump`, luego `docker compose cp db:/tmp/carbon.dump ./carbon.dump`. Conservar fuera del repositorio y probar la restauración en otra base. No ejecutar `docker compose down -v`: elimina el volumen.
