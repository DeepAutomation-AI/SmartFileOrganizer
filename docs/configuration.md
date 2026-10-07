# Configuración

La CLI carga `.yaml`, `.yml` o `.json` con el mismo esquema. Selección: `--config` tiene prioridad sobre `SFO_CONFIG`; sin ambos se usa `config/default.yaml`, relativo al directorio de trabajo. Usa las opciones globales antes del subcomando.

```bash
smartfileorganizer --config /ruta/config.yaml --env-file /ruta/.env validate
smartfileorganizer --config /ruta/config.yaml organize --mode dry-run
```

El cargador valida tipos, valores y claves desconocidas. Los nombres de reglas deben ser únicos; las claves duplicadas en YAML/JSON se rechazan. El límite de tamaño del archivo de configuración es 1 MiB. `validate` comprueba la configuración y muestra las rutas efectivas; no certifica que todos los archivos sean accesibles o permanezcan estables durante una ejecución.

## Rutas y fuentes

```yaml
sources:
  - path: "${HOME}/Downloads"
    recursive: true
  - path: "${HOME}/Desktop"
    recursive: false
destination_root: "${HOME}/Organized"
database: "../.state/history.sqlite3"
log_file: "../.logs/organizer.jsonl"
```

Las rutas relativas se resuelven desde el directorio del YAML/JSON. Se expanden `~`, `$VARIABLE` y `${VARIABLE}` solamente en las rutas; una variable no definida produce un error. En Windows puedes usar `${USERPROFILE}/Downloads` o rutas con `/`.

`sources` debe ser una lista no vacía. Cada entrada admite `path` y `recursive`, que por defecto es `true`; también se admite una cadena de ruta como abreviatura. `recursive: false` examina únicamente los archivos del primer nivel. Los enlaces simbólicos se omiten y el árbol de destino se excluye para evitar volver a organizar los resultados. Evita cambiar enlaces o permisos de las carpetas mientras se está organizando.

`destination_root` delimita los destinos permitidos. Una regla define un **directorio relativo**: se añade el nombre original del archivo. Las rutas absolutas, `..`, separadores `\`, unidades Windows y plantillas arbitrarias se rechazan. La comprobación del destino resuelto también impide salir de la raíz mediante un enlace simbólico.

`database` y `log_file` se crean al ejecutar movimientos reales. El modo `dry-run` no crea directorios, historial ni logs. Las bases de datos distintas implican bloqueos distintos; utiliza la misma ruta de base de datos si varios procesos organizan las mismas fuentes.

## Reglas

```yaml
rules:
  - name: Facturas PDF recientes
    enabled: true
    match:
      extensions: [pdf]
      keywords: [factura, invoice]
      max_size: 10485760
      newer_than_days: 30
    destination: "Documents/Invoices/{year}/{month}"
  - name: Archivos creados hace más de un año
    match:
      created_older_than_days: 365
    destination: "Archive/{year}"
  - name: Resto
    match: {}
    destination: Other
```

Se evalúan las reglas en orden y gana la primera habilitada que coincide. Todas las condiciones de una regla se combinan mediante AND. Las listas de `patterns`, `extensions`, `mime` o `keywords` admiten cualquiera de sus elementos, mediante OR; también aceptan una sola cadena. `enabled` es `true` por defecto y `match: {}` coincide con todos los archivos admitidos por el escáner.

| Condición | Semántica | Ejemplo |
| --- | --- | --- |
| `patterns` | Glob sobre el nombre completo, sensible a mayúsculas | `["*.pdf", "report-*.txt"]` |
| `extensions` | Extensión final, normalizada a minúsculas, con o sin punto | `[pdf, ".docx"]` |
| `mime` | MIME inferido mediante `mimetypes`; admite glob | `["image/*", "application/pdf"]` |
| `keywords` | Subcadena del nombre, sin distinción de mayúsculas | `[factura, invoice]` |
| `min_size` | Tamaño mínimo inclusivo, en bytes | `5242880` |
| `max_size` | Tamaño máximo inclusivo, en bytes | `10485760` |
| `older_than_days` | Antigüedad de modificación estrictamente superior | `90` |
| `modified_older_than_days` | Igual a `older_than_days`, con nombre explícito | `90` |
| `created_older_than_days` | Antigüedad de creación estrictamente superior | `365` |
| `newer_than_days` | Antigüedad de modificación estrictamente inferior | `7` |

Los tamaños deben ser enteros no negativos y `min_size` no puede superar `max_size`. La antigüedad admite números no negativos y un día equivale a 86.400 segundos. Un archivo sin regla coincidente no se mueve. Para archivos `.PDF`, usa `extensions: [pdf]` si necesitas coincidencia sin distinguir mayúsculas.

La creación usa `st_birthtime` si la plataforma lo proporciona; de lo contrario, usa la modificación. En Linux, `ctime` refleja cambios de metadatos y no se emplea como fecha de creación. El MIME se obtiene por extensión, no mediante lectura del formato interno.

## Plantillas de destino

| Campo | Valor |
| --- | --- |
| `{year}` / `{año}` | Año de modificación UTC, cuatro dígitos |
| `{month}` | Mes UTC, dos dígitos |
| `{day}` | Día UTC, dos dígitos |
| `{extension}` | Extensión final sin punto y en minúsculas |
| `{stem}` | Nombre sin la última extensión |
| `{name}` | Nombre completo del archivo |

Ejemplo: `Documents/PDFs/{year}/{month}` organiza `factura.pdf` modificado en octubre de 2026 como `Documents/PDFs/2026/10/factura.pdf`. `{name}` y `{stem}` crean un directorio con ese valor; el nombre del archivo se añade después. No se admiten conversiones, especificadores de formato ni acceso a atributos en las plantillas.

## Duplicados y colisiones

```yaml
duplicate_policy: skip
duplicate_directory: Duplicates
```

Se comparan el tamaño y el SHA-256 para identificar archivos de contenido idéntico. La deduplicación considera archivos presentes en el árbol de destino y decisiones de la ejecución actual. Coincidir en nombre o tamaño por sí solo no demuestra duplicación.

| Política | Resultado |
| --- | --- |
| `skip` | Mantiene el archivo duplicado en el origen; es el valor predeterminado |
| `quarantine` | Mueve el duplicado dentro de `destination_root/duplicate_directory` |
| `keep` | Organiza también el duplicado, utilizando un nombre disponible |

`duplicate_directory` es un directorio relativo sin plantillas. Si ya existe el nombre previsto, se elige otro mediante un sufijo. También se resuelven así las colisiones de nombres entre archivos de contenido diferente. El sistema no sobrescribe archivos existentes y verifica la copia antes de retirar el origen.

## Exclusiones y estabilidad

```yaml
exclude: ["*.part", "*.crdownload", "*.tmp"]
min_file_age_seconds: 2
```

`exclude` contiene patrones glob de nombres. `min_file_age_seconds` evita procesar archivos modificados demasiado recientemente. Los cambios de tamaño o metadatos durante la lectura y verificación impiden continuar el movimiento. Para descargas grandes, aumenta la edad mínima o excluye sus extensiones temporales.

Los errores de un archivo no detienen el resto del escaneo. Un resumen con errores provoca un código de salida distinto de cero en `organize`; consulta el log y el historial para resolver el caso y vuelve a ejecutar. Las rutas largas siguen sujetas a los límites del sistema operativo: un error se informa, no se trunca el nombre.

Las comprobaciones de metadatos y hashes detectan cambios observables, pero no bloquean obligatoriamente a otras aplicaciones. No organices archivos abiertos que otra aplicación pueda modificar o reemplazar; espera a cerrar esos archivos. Mantén las carpetas de origen y destino bajo tu control y evita sustituir rutas durante una ejecución.

## Persistencia y logs

SQLite guarda el historial de operaciones reales y la configuración usada, sin incorporar las credenciales del entorno. El registro de historial no modifica el YAML/JSON: este sigue siendo la fuente de configuración para cada nuevo proceso. `history --limit 20` consulta las operaciones más recientes y devuelve una lista vacía si no existe la base de datos.

La base usa `journal_mode=DELETE` y `synchronous=FULL`. Las consultas de historial y simulaciones no crean archivos laterales WAL/SHM. Antes de mover se persiste una entrada `moving`, que cambia a `moved`, `skipped` o `error` cuando se conoce el resultado. Si el proceso termina abruptamente, `moving` exige inspección manual del origen, el destino y el SHA-256 registrado; no acredita que el movimiento haya terminado. Consulta la [guía de recuperación operativa](deployment.md#backup-actualizaciones-y-paradas-abruptas).

Los logs se escriben como JSON por línea y usan rotación automática. Guarda `.state/` y `.logs/` fuera del control de versiones y utiliza almacenamiento local con permisos de lectura/escritura para el usuario del servicio. Las rutas en logs e historial pueden contener nombres de archivos personales; aplica los permisos adecuados a estos directorios.

`dry-run` evalúa reglas, hashes y nombres de destino, pero no escribe logs, SQLite o carpetas de salida, ni envía notificaciones. Una simulación no reserva los nombres: revisa de nuevo las decisiones si los archivos han cambiado antes de ejecutar.

## Notificaciones

```yaml
notifications:
  enabled: true
  channels: [desktop, email, telegram]
  timeout_seconds: 10
  email:
    ssl: false
```

`channels: []` desactiva los canales; `enabled: false` desactiva el conjunto. `timeout_seconds` debe ser mayor que cero y como máximo 60. Las notificaciones envían el resumen de una ejecución real. Un fallo del transporte se registra sin interrumpir los movimientos ni exponer el texto del error que pudiera incluir credenciales.

| Canal | Requisitos |
| --- | --- |
| `desktop` | Extra `desktop` (`plyer`) y una sesión gráfica con servicio de notificaciones |
| `email` | `SMTP_HOST`, `SMTP_FROM`, `SMTP_TO`; puerto `SMTP_PORT`, por defecto 587 con STARTTLS |
| `telegram` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`; acceso HTTPS a `api.telegram.org` |

`SMTP_TO` admite direcciones separadas por comas. Para autenticación, define tanto `SMTP_USERNAME` como `SMTP_PASSWORD`. Con `notifications.email.ssl: true` se usa TLS desde la conexión y el puerto predeterminado es 465; ajusta también `SMTP_PORT` en `.env` si tiene un valor explícito. Ambos transportes verifican los certificados TLS.

Los valores secretos se leen del entorno al enviar, nunca del YAML/JSON. El archivo definido por `--env-file` se carga sin reemplazar variables ya exportadas; si no existe, puedes usar únicamente variables del sistema. No pegues tokens o contraseñas en la configuración ni en argumentos CLI.

## Programación y observación

```yaml
schedule:
  interval_seconds: 300
  timezone: UTC
```

`schedule --apply` ejecuta APScheduler en primer plano hasta Ctrl-C. `interval_seconds` establece el intervalo y `timezone` identifica una zona horaria IANA. La zona del planificador no cambia las plantillas de fechas, que siempre usan UTC. `--apply` es obligatorio: sin él, el comando falla inmediatamente. Revisa previamente el plan con `organize --mode dry-run`.

También puedes usar una expresión cron de cinco campos dentro de APScheduler; `cron` e `interval_seconds` son mutuamente excluyentes:

```yaml
schedule:
  cron: "0 8 * * 1-5"
  timezone: America/Lima
```

Este ejemplo programa los días laborables a las 08:00 de la zona indicada. En Windows se instala `tzdata` como dependencia para proporcionar la base de zonas IANA. Si tu distribución Linux carece de ella, instala el paquete de zonas horarias del sistema o `tzdata` en el virtualenv.

`watch --apply` requiere el extra `realtime` (`watchdog`), observa las carpetas configuradas y respeta la misma configuración y estabilidad de archivos. También exige `--apply` y falla sin esa opción; no ofrece un modo de observación con simulaciones. Utiliza `organize --mode dry-run` para revisar decisiones. Consulta [deployment.md](deployment.md) para ejecutar estos procesos como servicio y para cron/systemd.
