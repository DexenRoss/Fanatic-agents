# Fanatic Agents

Fanatic Agents es una plataforma experimental para orquestar agentes de IA especializados con permisos explícitos, autonomía limitada y entrega de cambios para revisión humana.


## Requisitos

- Python 3.12 o posterior
- `pip`
- Git, Docker y GitHub CLI (`gh`) son recomendables y se comprueban con `doctor`
- GitHub CLI autenticado es obligatorio para `workflow deliver` y `workflow observe`
- `OPENAI_API_KEY` solo es necesaria para comandos con `--ai`; delivery no la usa

## Instalación

En Linux o macOS:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
```

En PowerShell:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .
```

Para desarrollo y ejecución de tests, instala el extra `dev`:

```bash
pip install -e ".[dev]"
```

## Uso

Consulta los comandos disponibles:

```bash
fanatic-agents --help
```

Diagnostica el entorno en modo read-only y comprueba también la autenticación de `gh`:

```bash
fanatic-agents doctor
```

Valida una configuración de proyecto:

```bash
fanatic-agents config validate projects/example.yaml
```

### Inspección de repositorios

La inspección local es determinística y no usa OpenAI:

```bash
fanatic-agents inspect /ruta/al/repositorio
```

El inspector acepta repositorios Git y directorios de proyecto sin Git. Detecta tecnologías, archivos importantes y posibles comandos de test/build, pero nunca ejecuta esos comandos. Incluye una selección pequeña y determinística de entry points y módulos fuente arquitectónicamente relevantes. Filtra secretos, binarios, dependencias, artefactos generados y symlinks; el contenido incluido queda acotado por límites de archivos fuente, por archivo y por snapshot.

Para solicitar una única evaluación estructurada del Developer Agent:

```bash
export OPENAI_API_KEY="..."
fanatic-agents inspect /ruta/al/repositorio --ai
```

`--ai` puede generar costos de API. En Sprint 1 el agente es de solo lectura, no tiene tools, recibe únicamente el snapshot filtrado, no ejecuta código y no modifica archivos del repositorio analizado. Sin `--ai`, no se requiere `OPENAI_API_KEY` y no existe ninguna llamada a OpenAI.

El modelo es opcional; si no se configura, se usa el default actual del Agents SDK:

```bash
export FANATIC_AGENTS_MODEL="..."
```

También puedes definir ambas variables en un archivo `.env` del directorio de trabajo, usando `.env.example` como plantilla. Fanatic Agents lo carga automáticamente; las variables exportadas en el entorno tienen precedencia. `.env` permanece ignorado por Git y nunca debe contener valores destinados al repositorio.

Ejecuta la suite de tests:

```bash
python -m pytest
```

### Sandbox Docker experimental

Comprueba que la CLI de Docker esta instalada y que el daemon responde:

```bash
fanatic-agents sandbox check
```

Ejecuta manualmente un comando permitido:

```bash
fanatic-agents sandbox run . \
  --image python:3.12-slim \
  --command "python --version"
```

La imagen debe existir localmente. Fanatic Agents nunca ejecuta `docker pull`: debes descargar o construir la imagen explicitamente antes de usarla.

El sandbox trabaja exclusivamente sobre una copia temporal, filtrada y acotada del repositorio. El repositorio original no se monta en el contenedor y no recibe los cambios efectuados durante la ejecucion. Se excluyen `.git`, entornos virtuales, dependencias, artefactos generados, symlinks, binarios y nombres de archivo que puedan contener credenciales, incluidos `.env`, `.env.*`, claves y tokens.

La red permanece deshabilitada. El contenedor usa limites conservadores de tiempo, memoria, CPU, procesos y salida; elimina capabilities, activa `no-new-privileges`, usa un root filesystem de solo lectura y recibe unicamente `HOME=/tmp` y `PYTHONDONTWRITEBYTECODE=1`. No se montan sockets, configuraciones personales ni credenciales, y `OPENAI_API_KEY` no se pasa al contenedor.

Esta funcionalidad es experimental. El Developer Agent continua sin tools y no tiene acceso al sandbox; Sprint 2 solo permite invocarlo manualmente desde la CLI o la API Python.


### Orquestación multi-agente read-only (Sprint 3)

La inspección local del workflow no usa OpenAI ni requiere una API key:

```bash
fanatic-agents workflow plan .
```

Para ejecutar una sola pasada estructurada de Planner, Developer Planning, Reviewer y QA:

```bash
export OPENAI_API_KEY="..."
fanatic-agents workflow plan . --ai
```

El modelo sigue siendo configurable mediante `FANATIC_AGENTS_MODEL`; no se fija un modelo concreto en código. `--ai` puede realizar como máximo cuatro llamadas al modelo: una por agente. Si un human gate o una condición de parada se activa, los agentes posteriores no se ejecutan. No existen retries, handoffs autónomos ni loops Developer/Reviewer.

Todo el Sprint 3 es read-only:

- los cuatro agentes usan outputs Pydantic y `tools=[]`;
- ningún agente edita archivos, ejecuta Git, usa GitHub ni accede a secretos;
- Developer Planning y QA solo proponen comandos como vectores `SandboxCommand`;
- el Orchestrator valida cada propuesta con `CommandPolicy`, pero nunca la ejecuta;
- comandos bloqueados, tareas de riesgo alto y decisiones sensibles detienen el workflow;
- la ejecución del sandbox continúa siendo exclusivamente manual mediante `sandbox run`.

Un resultado `READY_FOR_IMPLEMENTATION` indica que el plan completó sus gates; no significa que se haya aplicado o ejecutado ningún cambio.

### Controlled Implementation (Sprint 4)

La fase explícita de implementación controlada se ejecuta así:

```bash
fanatic-agents workflow implement . --image python:3.12-slim --ai
```

El comando ejecuta Planner, Developer Planning, Reviewer, QA e Implementation solo si cada gate anterior se aprueba, por lo que realiza como máximo cinco llamadas al modelo. Implementation Agent tiene `tools=[]`: no recibe filesystem, shell, Git ni Docker y únicamente produce un `ChangeSet` Pydantic con operaciones `create`, `modify` y `delete` usando contenido completo de archivo.

Antes de cualquier escritura, `ChangePolicy` valida atómicamente paths, alineación con `files_likely_affected`, symlinks, estado previo y límites (10 archivos, 50.000 caracteres por archivo, 150.000 caracteres totales y 2 eliminaciones). Paths inseguros, `.git`, `.env`, credenciales y Docker socket se rechazan; `AGENTS.md`, workflows de CI, deployment, infraestructura, autenticación y migraciones sensibles requieren intervención humana.

Los cambios se aplican con Python, sin `patch`, `git apply`, shell ni scripts generados por el modelo, exclusivamente dentro de una copia temporal filtrada. Los comandos de verificación proceden únicamente del `QAPlan`, se revalidan con `CommandPolicy` y se ejecutan con el Docker Sandbox endurecido sobre ese mismo workspace ya modificado. La imagen debe indicarla el usuario, existir localmente y nunca se descarga automáticamente.

El repositorio original permanece intacto: los cambios temporales no se copian de vuelta, no se crea branch, commit, push ni PR. El workspace se destruye al terminar. Si la verificación falla, el resultado es `VERIFICATION_FAILED` y no existe correction loop, retry autónomo ni una segunda llamada a Implementation Agent.

Sin `--ai`, el comando solo inspecciona localmente e informa `AI implementation: NOT REQUESTED`; no llama agentes ni ejecuta Docker.

### Verified Change Promotion (Sprint 5)

Un `ChangeSet` que termina en `VERIFIED` puede conservarse solo mediante la
autorización humana explícita `--promote`:

```text
VERIFIED
  -> explicit --promote
  -> new local fanatic/* branch
  -> dedicated Git worktree
  -> exact verified ChangeSet, uncommitted
```

Ejemplo:

```bash
fanatic-agents workflow implement PROJECT \
  --image python:3.12-slim \
  --ai \
  --promote \
  --branch fanatic/task-name
```

La promoción exige que `PROJECT` sea la raíz de un repositorio Git, que
`HEAD` no esté detached, que el working tree original esté limpio y que el
branch y commit sigan siendo exactamente los registrados antes de Controlled
Implementation. El nombre de rama lo proporciona el usuario, debe ser un ref
Git válido bajo `fanatic/` y no puede existir previamente. `--promote`
requiere `--branch`, y `--branch` sin `--promote` se rechaza.

Fanatic Agents crea el worktree fuera del repositorio original, con un layout
hermano equivalente a:

```text
<repo-parent>/
  project/
  .fanatic-agents-worktrees/
    project/
      fanatic-task-name-<branch-hash>/
```

Antes de crear recursos se vuelve a ejecutar `ChangePolicy`. El worktree nace
desde el SHA exacto registrado, se aplica el mismo `ChangeSet` ya verificado y
se comprueban el contenido exacto de cada create/modify/delete y el conjunto
estricto de paths informado por Git. La promoción no llama nuevamente al
modelo, no ejecuta tests en el worktree persistente y no relaja
`PermissionsConfig`.

La rama nueva continúa apuntando al commit base. Los cambios verificados viven
intencionalmente como cambios **sin commit** en su worktree para revisión
humana. No se ejecuta `git add`, commit, push, Pull Request, merge ni rebase.
El repositorio original conserva su branch, `HEAD`, limpieza y archivos.

Revisión manual:

```bash
cd <promotion-worktree>
git status
git diff
```

Para descartar manualmente una promoción exitosa, desde el repositorio original:

```bash
git worktree remove --force <promotion-worktree>
git branch -d fanatic/task-name
```

Fanatic Agents no elimina automáticamente worktrees exitosos. Si una promoción
falla después de crear recursos, elimina únicamente ese worktree fallido y la
rama nueva creada por esa misma operación.


### Git Delivery (Sprint 6)

Sprint 6 mantiene un human review gap obligatorio y añade un comando separado:

```text
VERIFIED
  -> PROMOTED
  -> revision humana del worktree
  -> workflow deliver
  -> un commit local
  -> push de la nueva rama fanatic/*
  -> Pull Request
  -> revision humana
```

Antes de entregar, instala GitHub CLI por separado y autentícalo. Fanatic
Agents no instala `gh`, no solicita PATs y no almacena tokens de GitHub:

```bash
gh auth login
fanatic-agents doctor
```

El remote debe llamarse `origin` y usar una URL GitHub HTTPS o SSH. La rama
debe ser la misma rama nueva `fanatic/*` creada por promoción. Un check
read-only está disponible antes de autorizar efectos:

```bash
fanatic-agents workflow deliver <promotion-worktree> --check
```

Entrega explícita:

```bash
fanatic-agents workflow deliver <promotion-worktree>
```

Se pueden proporcionar `--commit-message` y `--pr-title`. Ambos valores son
subjects de una sola línea y se pasan como argumentos, nunca al shell. Sin
overrides, el mensaje y el título se derivan determinísticamente del título de
la tarea, sin otra llamada al modelo. Delivery no requiere
`OPENAI_API_KEY` y añade **cero llamadas a OpenAI**.

Al promover, Fanatic Agents crea un `PromotionReceipt` JSON fuera del
repositorio y del worktree, bajo
`.fanatic-agents-worktrees/.metadata/`. El recibo enlaza repository, rama
base, SHA base, rama promovida, worktree, tarea, operaciones, paths, hashes
SHA-256 y resumen acotado de verificación. No guarda prompts completos,
variables de entorno, stdout de tests ni secrets.

`workflow deliver` falla cerrado si el recibo falta o está corrupto, si el
worktree ya no está registrado, si branch/HEAD/repository no coinciden o si
existe cualquier cambio manual o path adicional. Para
create/modify compara hashes; para delete exige ausencia; después compara el
conjunto exacto de `git status --porcelain`. El staging usa exclusivamente:

```text
git add --all -- <verified-path-1> <verified-path-2> ...
```

Nunca usa `git add .`. Después verifica
`git diff --cached --name-status`, crea un solo commit sin `--no-verify` y
confirma que su parent es el SHA base. Sólo entonces comprueba que la rama
remota no exista y ejecuta `git push --set-upstream origin <fanatic-branch>`
sin force. Finalmente usa `gh pr create` con la rama base registrada; nunca
elige `main` por defecto si la promoción nació de otra rama.

Si ya existe configuración de proyecto, puede exigirse con `--config`:

```bash
fanatic-agents workflow deliver <promotion-worktree> \
  --config projects/example.yaml
```

`permissions.commit`, `permissions.push_branch` y
`permissions.create_pull_request` deben estar habilitados; sus defaults
siguen siendo `false`. Ejecutar el comando es la autorización humana
explícita cuando no se proporciona una configuración.

El receipt registra atómicamente `promoted`, `commit_created`,
`branch_pushed` y `pr_created`. Un lock local impide dos deliveries
simultáneos. Si falla push se conserva el commit; si falla el PR se conserva la
rama remota; una ejecución posterior continúa sólo la etapa pendiente y
reutiliza un PR existente. No hay retry loops automáticos ni recovery
destructivo.

Delivery termina en `DELIVERED_FOR_REVIEW`. No existe force push, merge
automático, borrado remoto, polling de CI ni deployment. El repositorio
original no cambia; commit y push ocurren exclusivamente en el promotion
worktree y el PR queda esperando revisión humana.


### Pull Request Observation (Sprint 7)

Sprint 7 añade una fase separada, determinística y de solo lectura después de
la entrega:

```text
DELIVERED_FOR_REVIEW
  -> OBSERVE
  -> WAITING_FOR_CI
  -> WAITING_FOR_REVIEW
  -> READY_FOR_HUMAN_MERGE
```

El comando obtiene repository, número de PR, ramas y SHA esperado desde el
`PromotionReceipt` externo creado por Sprint 6; no es necesario volver a
introducir owner, repository ni número:

```bash
fanatic-agents workflow observe <promotion-worktree>
```

La observación comprueba primero la provenance completa: repository y número
codificados en la URL, branch base, branch `fanatic/*` y el SHA exacto del
único commit entregado. Un push posterior produce `PR_HEAD_DRIFTED`; Fanatic
Agents no evalúa ni corrige esos commits nuevos.

`gh pr view --json` aporta estado, draft, mergeability, review decision,
reviews y `statusCheckRollup`. Se observan todos los proveedores reportados
por GitHub (Actions, linters, escáneres y checks externos). Failure, timeout,
action-required o cancelación producen `CI_FAILED`; estados pendientes o
desconocidos producen `WAITING_FOR_CI`; checks terminados con success, skipped
o neutral producen `CI_PASSED`; una lista vacía produce `NO_CI_REPORTED` y
nunca se interpreta como CI aprobado.

Después de CI verde, approvals producen `READY_FOR_HUMAN_MERGE`; review
pendiente produce `WAITING_FOR_REVIEW`, y `CHANGES_REQUESTED` detiene la
observación. También son estados explícitos `PR_DRAFT`, `MERGE_CONFLICT`,
`PR_CLOSED`, `MERGED_EXTERNALLY`, `INVALID_DELIVERY`, `GITHUB_UNAVAILABLE` y
`OBSERVATION_FAILED`. `MERGED_EXTERNALLY` solo informa que un humano u otra
herramienta realizó el merge; Fanatic Agents no se atribuye esa acción.

Hay polling local opcional, siempre acotado y sin threads ni scheduler:

```bash
fanatic-agents workflow observe <promotion-worktree> \
  --watch \
  --interval-seconds 30 \
  --timeout-seconds 600
```

El intervalo mínimo es 10 segundos y el timeout máximo es 1800 segundos. Al
vencer, se devuelve el último snapshot sin rerun de CI, retry autónomo,
comentario ni edición de código. El snapshot normalizado más reciente se
persiste junto al receipt en `.fanatic-agents-worktrees/.metadata/`; no guarda
la respuesta completa de GitHub, logs, tokens, variables de entorno ni
secrets.

Con `--config`, `permissions.observe_pull_request` debe estar habilitado; sin
configuración, ejecutar manualmente `observe` constituye autorización humana
explícita de lectura. Esta fase hace **cero llamadas al modelo**, no requiere
`OPENAI_API_KEY`, no invoca Git y no modifica repository, worktree, index,
branch, commit, remote ni Pull Request.

**Fanatic Agents todavía NO realiza merge automático.**
`READY_FOR_HUMAN_MERGE` es exclusivamente información para conservar el gate
humano.


### GitHub Issue Task Intake (Sprint 8)

Sprint 8 añade descubrimiento y selección local de trabajo autorizado, sin
conectarlo todavía al workflow de implementación:

```text
GitHub Issue
  -> label fanatic:ready
  -> TaskIntakePolicy
  -> deterministic selection
  -> local TaskIntakeReceipt
  -> TASK_SELECTED
  -> STOP
```

Los comandos son independientes de `workflow implement`:

```bash
fanatic-agents task discover /ruta/al/repositorio
fanatic-agents task select /ruta/al/repositorio
```

El path debe pertenecer a un repositorio Git con `origin` GitHub HTTPS o SSH.
Fanatic Agents deriva `OWNER/REPO` del remote y usa únicamente
`gh issue list --state open --limit ... --json ...`. No solicita ni persiste
un PAT. El backlog está acotado a 50 Issues por defecto y a un máximo
configurable de 100.

La autorización es opt-in y fail-closed. Por defecto una Issue necesita
`fanatic:ready`; `fanatic:blocked` o `fanatic:manual` impiden su selección.
Los labels requeridos y bloqueados son configurables por proyecto. Con
`--config`, tanto `intake.enabled` como `permissions.read_issues` deben
estar habilitados. Sin config, la ejecución manual autoriza solo esa operación
de lectura.

La selección no usa un LLM. Ordena localmente y sin depender del orden de
GitHub: `priority:p0`, `p1`, `p2`, `p3`, sin prioridad; después la
Issue más antigua y finalmente el número menor. Una Issue con varios labels
de prioridad es inválida y no se selecciona. `select` siempre hace un fetch
nuevo, por lo que respeta cierres o cambios de labels ocurridos después de
`discover`.

Título y body son contenido **no confiable**. Nunca se interpretan como
comandos, configuración, permisos, paths ni instrucciones para agentes. El body
se trunca explícitamente a 20.000 caracteres y el modelo conserva
`source_content_trusted=false`. Sprint 8 realiza cero llamadas a OpenAI,
Docker, Planner, Developer, Reviewer o QA.

`select` captura branch y HEAD actuales sin hacer checkout, pull ni exigir un
working tree limpio. Persiste una reserva local `TaskIntakeReceipt` fuera del
repositorio, bajo `.fanatic-agents-worktrees/.metadata/intake/`. El receipt no
contiene el body, payload raw, prompts, environment, tokens ni secrets. Un
receipt activo evita seleccionar dos veces la misma Issue; metadata corrupta
falla cerrada y nunca se elimina silenciosamente.

Task intake no ejecuta `git add`, commit, push, merge, checkout ni switch. No
edita, comenta, cierra, asigna o etiqueta Issues; tampoco crea branches, PRs o
merges. `TASK_SELECTED` significa exclusivamente que el trabajo quedó
reservado localmente. No existe scheduler, cron, daemon ni inicio automático
de implementación. Una fase futura podrá consumir el `TaskSpec` después de
volver a validar repository, branch, SHA, autorización y estado de la Issue.


## Configuración

Las configuraciones YAML se validan estrictamente: no se aceptan campos desconocidos, tipos implícitos incorrectos ni límites menores o iguales que cero. Consulta [`projects/example.yaml`](projects/example.yaml) como referencia.

## Política de seguridad

Fanatic Agents nunca debe realizar automáticamente las siguientes acciones sin autorización humana explícita:

- merge a la rama principal;
- deploy a producción;
- modificación de secrets;
- operaciones destructivas de base de datos.

Sus permisos tienen defaults conservadores y esas cuatro operaciones peligrosas permanecen desactivadas salvo configuración explícita. El Developer Agent de Sprint 1 no recibe herramientas y no puede modificar el repositorio.

