# Deployment local

SmartFileOrganizer es un proceso local que debe ejecutarse con el usuario que tiene acceso a las carpetas de origen y destino. Funciona con Python 3.11 o posterior en Linux, macOS y Windows. APScheduler y watchdog se ejecutan en primer plano; cron y systemd pueden gestionar sus ejecuciones.

## Entorno de desarrollo

```bash
cd /ruta/SmartFileOrganizer
python3 -m venv .venv
uv sync --locked --extra dev
cp .env.example .env
.venv/bin/smartfileorganizer --config config/default.yaml validate
.venv/bin/smartfileorganizer --config config/default.yaml organize --mode dry-run
.venv/bin/python -m ruff check src tests
.venv/bin/python -m pytest
```

Con `uv` instalado, `sync --locked` utiliza `uv.lock` y rechaza una divergencia respecto de `pyproject.toml` sin modificar el lock. Para incluir watchdog, ejecuta `uv sync --locked --extra dev --extra realtime`. También puedes usar `make sync` y `make sync-realtime`. La CI repite la instalación bloqueada, lint y las pruebas en Python 3.11, 3.12 y 3.13.

La alternativa sin uv es `.venv/bin/python -m pip install -e '.[dev]'` o `make install`; instala versiones que satisfacen los rangos declarados. Para instalar una versión bloqueada específica de Python, selecciona ese intérprete al crear el virtualenv o utiliza `uv sync --locked --python 3.11 --extra dev`.

En tareas de Codex, utiliza el checkout existente bajo `/workspace`: cada tarea ya se ejecuta en un entorno aislado y no necesita un worktree adicional, salvo que se solicite expresamente. Los comandos de instalación no modifican fuentes, pruebas ni ejemplos de configuración; los directorios `.venv`, `.state` y `.logs` son locales y están ignorados por Git.

Para el flujo normal se necesitan Python, las dependencias de `pyproject.toml`, carpetas legibles y un destino escribible. La validación del esquema, las pruebas y el lint son comprobaciones de desarrollo; escritorio, email, Telegram, Docker y observación en tiempo real son opcionales. La instalación desde PyPI requiere acceso a `pypi.org` y `files.pythonhosted.org`; el funcionamiento sin notificaciones no requiere red.

## Instalación para uso continuo

En los ejemplos siguientes, sustituye `/home/alice` por tu carpeta personal. Mantén la configuración, el entorno y los datos fuera del paquete para poder actualizarlo sin perder el historial.

```bash
python3 -m venv /home/alice/.local/share/smartfileorganizer/venv
/home/alice/.local/share/smartfileorganizer/venv/bin/python -m pip install /ruta/SmartFileOrganizer
mkdir -p /home/alice/.config/smartfileorganizer
mkdir -p /home/alice/.local/state/smartfileorganizer
cp /ruta/SmartFileOrganizer/config/default.yaml /home/alice/.config/smartfileorganizer/config.yaml
cp /ruta/SmartFileOrganizer/.env.example /home/alice/.config/smartfileorganizer/.env
chmod 700 /home/alice/.config/smartfileorganizer /home/alice/.local/state/smartfileorganizer
chmod 600 /home/alice/.config/smartfileorganizer/.env
```

Edita las rutas del archivo copiado. Las rutas predeterminadas `../.state` y `../.logs` son relativas a la configuración; para esta instalación resulta más claro usar rutas explícitas:

```yaml
database: /home/alice/.local/state/smartfileorganizer/history.sqlite3
log_file: /home/alice/.local/state/smartfileorganizer/organizer.jsonl
```

Prueba primero la simulación y después una ejecución real:

```bash
/home/alice/.local/share/smartfileorganizer/venv/bin/smartfileorganizer --config /home/alice/.config/smartfileorganizer/config.yaml --env-file /home/alice/.config/smartfileorganizer/.env organize --mode dry-run
/home/alice/.local/share/smartfileorganizer/venv/bin/smartfileorganizer --config /home/alice/.config/smartfileorganizer/config.yaml --env-file /home/alice/.config/smartfileorganizer/.env organize --mode interactive
```

Los comandos que cargan configuración o consultan un historial inexistente no crean estado. Las ejecuciones reales necesitan permiso para crear el destino, la base SQLite, su lockfile y el log.

## APScheduler

Configura `schedule.interval_seconds` y `schedule.timezone`; ejecuta:

```bash
/home/alice/.local/share/smartfileorganizer/venv/bin/smartfileorganizer --config /home/alice/.config/smartfileorganizer/config.yaml --env-file /home/alice/.config/smartfileorganizer/.env schedule --apply
```

El proceso permanece activo hasta Ctrl-C. La configuración se carga al arrancar: reinícialo para aplicar cambios. No hace falta persistir trabajos APScheduler, porque se reconstruye la programación desde el archivo. Los trabajos no deben ejecutarse en paralelo sobre las mismas fuentes con bases de datos diferentes.

`schedule` y `watch` rechazan la ejecución sin `--apply`. Antes de activarlos, revisa `organize --mode dry-run`; con Make, utiliza `make schedule APPLY=--apply` o `make watch APPLY=--apply`.

## Cron

Edita el crontab del usuario propietario de los archivos con `crontab -e`. Usa rutas absolutas porque cron no activa el virtualenv ni garantiza tu directorio de trabajo:

```cron
*/5 * * * * /home/alice/.local/share/smartfileorganizer/venv/bin/smartfileorganizer --config /home/alice/.config/smartfileorganizer/config.yaml --env-file /home/alice/.config/smartfileorganizer/.env organize --mode automatic >> /home/alice/.local/state/smartfileorganizer/cron.log 2>&1
```

El lockfile junto a la base SQLite evita ejecuciones reales simultáneas que usen la misma base. Un segundo proceso informa del bloqueo; el bloqueo del sistema operativo se libera al terminar el primero, incluso si permanece el archivo `.lock`. No borres ese archivo mientras se esté ejecutando el programa.

`cron.log` contiene la salida del proceso y no utiliza la rotación del log JSON de la aplicación; configúralo con `logrotate` o redirígelo a la infraestructura de logs de tu sistema si vas a mantener cron a largo plazo. No actives simultáneamente cron y otro planificador salvo que necesites ambos conscientemente.

## Servicio systemd de usuario

Crea `~/.config/systemd/user/smartfileorganizer.service`:

```ini
[Unit]
Description=Organizador de archivos SmartFileOrganizer

[Service]
Type=simple
WorkingDirectory=/home/alice
ExecStart=/home/alice/.local/share/smartfileorganizer/venv/bin/smartfileorganizer --config /home/alice/.config/smartfileorganizer/config.yaml --env-file /home/alice/.config/smartfileorganizer/.env schedule --apply
Restart=on-failure
RestartSec=10
UMask=0077

[Install]
WantedBy=default.target
```

Activa y consulta el servicio:

```bash
systemctl --user daemon-reload
systemctl --user enable --now smartfileorganizer.service
systemctl --user status smartfileorganizer.service
journalctl --user -u smartfileorganizer.service -f
```

Después de cambiar la configuración, ejecuta `systemctl --user restart smartfileorganizer.service`. Para detenerlo: `systemctl --user disable --now smartfileorganizer.service`. Un servicio de usuario suele depender de que exista una sesión del usuario; si necesitas que se mantenga sin sesión, configura el mecanismo de persistencia de sesiones de systemd según las políticas de tu máquina.

## Tiempo real con watchdog

Instala el extra en el mismo virtualenv:

```bash
/home/alice/.local/share/smartfileorganizer/venv/bin/python -m pip install '/ruta/SmartFileOrganizer[realtime]'
/home/alice/.local/share/smartfileorganizer/venv/bin/smartfileorganizer --config /home/alice/.config/smartfileorganizer/config.yaml --env-file /home/alice/.config/smartfileorganizer/.env watch --apply
```

Para utilizarlo con systemd, cambia `schedule --apply` por `watch --apply` en `ExecStart`. Los eventos se agrupan y los archivos recientes/inestables se omiten hasta que puedan evaluarse con seguridad. Usa un intervalo de estabilidad suficiente para tus aplicaciones y conserva un escaneo periódico si necesitas recuperar cambios no observados durante una parada del servicio.

Las notificaciones de escritorio requieren el extra `desktop`, una sesión gráfica y los servicios de notificación de la plataforma. En un servidor, contenedor o servicio sin sesión gráfica, usa email/Telegram o los logs.

## Docker

La imagen instala el extra `realtime`, usa Python 3.11 y ejecuta por defecto una simulación como usuario sin privilegios. Para persistir archivos y SQLite, monta un directorio del host y coloca allí la configuración:

```bash
cd /ruta/SmartFileOrganizer
docker build -t smartfileorganizer:local .
mkdir -p runtime/config runtime/Downloads runtime/Desktop
cp config/default.yaml runtime/config/default.yaml
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/data -v "$PWD/runtime:/data" smartfileorganizer:local --config /data/config/default.yaml organize --mode dry-run
```

La configuración copiada resuelve `${HOME}` como `/data` y guarda el historial en `/data/.state/` y los logs en `/data/.logs/`, que persisten en `runtime/`. Sustituye las carpetas de ejemplo por tus fuentes o monta sus rutas explícitamente y ajusta el YAML.

Para aplicar movimientos periódicos:

```bash
docker run -d --name smartfileorganizer --restart unless-stopped --user "$(id -u):$(id -g)" -e HOME=/data -v "$PWD/runtime:/data" smartfileorganizer:local --config /data/config/default.yaml schedule --apply
docker logs -f smartfileorganizer
docker stop smartfileorganizer
```

Si necesitas secretos, utiliza `--env-file /ruta/protegida/.env` en `docker run`, antes del nombre de la imagen. Docker no evalúa las mismas reglas de expansión que `python-dotenv`, así que usa valores literales en ese archivo. `.dockerignore` excluye `.env` y los datos locales. Las notificaciones de escritorio no están habilitadas en la imagen.

El Dockerfile instala dependencias según los rangos de `pyproject.toml`; no utiliza `uv.lock`. Una nueva construcción puede resolver versiones distintas. Conserva la imagen validada por su identificador o digest si necesitas desplegar exactamente la misma imagen.

## Backup, actualizaciones y paradas abruptas

Detén el organizador antes de copiar el historial para obtener un backup coherente, o utiliza la API de backup de SQLite. La base utiliza journal `DELETE` y sincronización `FULL`. Conserva la configuración y los archivos organizados junto con el historial: este registra acciones y no contiene el contenido de los documentos.

Para actualizar, detén el servicio, instala la nueva versión en el virtualenv, ejecuta `validate` y `organize --mode dry-run` con la configuración existente y vuelve a arrancar. En un checkout utiliza el `uv.lock` de la versión elegida con `uv sync --locked`; la alternativa pip resuelve los rangos de dependencias y puede elegir versiones distintas.

El movimiento registra `moving` en SQLite antes de copiar, verifica la copia, publica un nombre exclusivo y elimina el origen al final. En POSIX se ejecuta `fsync` sobre el directorio de destino antes de eliminar el origen y sobre el directorio de origen después. En Windows no existe una operación portable equivalente para sincronizar directorios; la protección ante una pérdida abrupta de energía depende también del sistema de archivos y del almacenamiento. Los errores de permisos, bloqueo o copia antes de eliminar el origen lo conservan. Si falla una operación después de retirarlo, se conserva el destino publicado y se informa del error.

Una parada abrupta puede dejar una entrada `moving`, una copia publicada además del origen o un archivo temporal `.smartfileorganizer-*` en el destino. No hay una transacción única entre SQLite y los archivos ni una recuperación automática. Detén todos los organizadores, consulta `history`, localiza ambas rutas y compara tamaño y SHA-256 con el historial antes de decidir cómo recuperar o limpiar. Conserva el archivo que contiene los datos verificados. Los temporales huérfanos y archivos desconocidos nunca se limpian automáticamente; no borres archivos solo por su nombre o por un estado incompleto del historial.

El bloqueo de ejecución coordina organizadores que usan la misma base, pero no bloquea obligatoriamente otras aplicaciones. Cierra los archivos que otra aplicación esté modificando o reemplazando y evita cambios externos de rutas durante el movimiento. La verificación de metadatos y SHA-256 no establece una transacción con esos procesos.

## Diagnóstico

| Problema | Comprobación y corrección |
| --- | --- |
| Fuente inexistente o sin permiso | Revisa la ruta efectiva con `validate` y los permisos del usuario del proceso |
| No coincide ninguna regla | Comprueba el orden, mayúsculas en `patterns`, extensión final y condiciones AND |
| Archivo reciente o cambia durante lectura | Espera a que termine la escritura o aumenta `min_file_age_seconds` |
| Destino o SQLite no escribible | Comprueba permisos de las carpetas padre y montajes del contenedor |
| Ejecución bloqueada | Identifica otro organizador con la misma base; espera o detén ese proceso |
| Path largo/archivo bloqueado | Cierra la aplicación que lo usa o acorta la ruta raíz; vuelve a ejecutar |
| No llega la notificación | Comprueba el canal, nombres de variables, dependencia opcional y salida TLS/HTTPS |
| `watch` no disponible | Instala el extra `realtime` en el virtualenv del servicio |
| El servicio usa reglas antiguas | Reinicia el proceso después de editar configuración o `.env` |

No desactives la verificación de certificados para corregir errores de instalación, SMTP o Telegram. Configura las autoridades de confianza del sistema cuando tu red utilice certificados corporativos. No imprimas `.env` ni vuelques el entorno completo al investigar credenciales.
