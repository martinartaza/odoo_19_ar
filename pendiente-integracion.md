# Pendientes, a propósito

Decisiones que se tomaron y después se postergaron, con el motivo y el disparador que
debería traer cada una de vuelta. Nada de esto es trabajo olvidado: es trabajo cuyo costo
hoy es mayor que su beneficio.

---

## 1. Que `main` solo se pueda modificar por PR

**Estado:** postergado el 2026-09-02.
**Disparador:** cuando la integración llegue a un punto estable y los cambios dejen de ser
frecuentes.

**Por qué se posterga.** Hoy casi todo cambio es infraestructura — hooks, skills, reglas de
permisos, la capa de despliegue. Forzar cada uno de esos por un pull request costaría más
de lo que protege mientras la forma de la cosa todavía se mueve. Es un juicio sobre el
*momento*, no sobre si el guardarraíl está bien.

**Por qué vuelve.** Una regla `deny` en `.claude/settings.json` solo limita al agente, y
solo la forma exacta del comando que nombra. Se sostuvo el 2026-09-02 porque el agente
eligió respetarla en vez de usar el `git push` a secas, que se le escapa al patrón. Eso no
es una garantía.

### Las tres capas, y qué cubre cada una

| Capa | Frena a | Se saltea con |
|---|---|---|
| Regla `deny` en `settings.json` | al agente, y solo esa forma del comando | escribir el comando de otra manera |
| Hook `pre-push` de git | a cualquiera en esta máquina | `--no-verify`, o un clon sin `core.hooksPath` |
| **Branch protection de GitHub** | **a todos, siempre** | nada local |

Una regla de permisos **no puede** expresar "rechazá si la rama es `main`": las reglas
matchean texto de comando, y `git push` no dice qué rama empuja — eso depende de dónde
estás parado y del upstream configurado. Git sí lo sabe, y por eso el control corresponde
ahí y no a un hook de Claude. Además, un hook de git es estrictamente mejor acá: limita a
cualquiera que ejecute el comando, no solo al agente.

### La mitad local: un hook `pre-push`

`.githooks/pre-push`:

```bash
#!/usr/bin/env bash
# Rechaza empujar main directamente. Git le pasa al hook una linea por ref:
#   <local ref> <local sha> <remote ref> <remote sha>
while read -r _ _ remote_ref _; do
    if [ "$remote_ref" = "refs/heads/main" ]; then
        echo "pre-push: no se empuja main directamente. Abri un PR." >&2
        exit 1
    fi
done
```

Se activa con:

```bash
chmod +x .githooks/pre-push
git config core.hooksPath .githooks
```

Ve las refs sin importar cómo se invocó el push — `git push`, `git push origin main`,
`git push --all` — porque git se las entrega al hook en vez de reconstruirlas desde la
línea de comando.

**Dos cosas que hay que resolver al activarlo:**

- El `.gitignore` arranca con un `.*` a secas, así que `.githooks/` quedaría ignorado
  exactamente como le pasó a `.claude/`. Necesita una excepción `!.githooks/`, o el hook
  existe solo en la máquina que lo escribió.
- `core.hooksPath` es **por clon**. No se hereda al clonar, así que hay que volver a
  configurarlo en el servidor y en cualquier otro lado donde caiga el repo.

### La mitad que realmente aguanta: branch protection

GitHub → Settings → Branches → Add rule sobre `main` → *Require a pull request before
merging*. Es del lado del servidor, así que ninguna configuración local la evade: ni
`--no-verify`, ni un clon sin el hook, ni un agente mal configurado.

El hook local atrapa errores antes de que salgan de la máquina. Branch protection es la
que vale igual.

---

## 2. Costo por corrida en la tarjeta de Trello

**Estado:** postergado.
**Disparador:** el runner, que es donde `claude -p --output-format json` devuelve el
consumo.

Mientras las corridas se lancen a mano no hay un límite por corrida, así que no hay a qué
atribuirle un número. Nota para cuando llegue: el consumo de un subagente **no** aparece
inline en el transcript del padre y hay que sumarlo aparte.

---

## 3. "No crear módulos nuevos" como hook

**Estado:** postergado.
**Disparador:** el runner, que es quien pone el `card_id` del ticket en el entorno.

No puede ser una regla `deny`: denegar escrituras bajo `custom_addons/` bloquearía también
el módulo que el pipeline tiene que editar, y `deny` no admite excepciones. Hasta que el
hook exista, la regla vive en `CLAUDE.md` como consejo y **no hay que darla por aplicada**.
Lo mismo vale para la otra mitad: negarse a borrar un módulo salvo que el ticket lleve
determinada etiqueta.

---

## 4. El backup antes de un pase, y por qué hoy no se puede restringir de verdad

**Estado:** postergado el 2026-09-02.
**Disparador:** el deploy por GitHub Actions, o un usuario SSH no-root para el pipeline.

**La idea, que es correcta.** Un agente debería poder **disparar** un backup pero no poder
**leerlo**. Separar "puedo ejecutar la operación" de "puedo ver el resultado". Crear el
backup es legítimo y necesario antes de cualquier pase a staging o producción; leerlo es
el riesgo real de exfiltración que la regla `deny` sobre `pg_dump` pretendía cubrir. Hoy
la regla confunde las dos cosas y bloquea un caso legítimo — pasó el 2026-09-02, al
rehacer producción sin poder tomar un backup fresco.

**Por qué la implementación ingenua no sirve.** El agente entra a producción como `root`.
Contra root no hay permiso de archivo que valga, y una regla `deny` sobre la ruta es la
capa más débil de todas: Bash matchea prefijos y se esquiva escribiendo el comando
distinto. **Una carpeta "que el agente no puede leer" es teatro mientras el agente tenga
root.**

**Lo que sí lo implementa**, por orden de fuerza:

1. **Que el agente no haga el deploy.** En GitHub Actions el backup es un paso del workflow
   con una identidad del servidor que el agente nunca tiene. La pregunta "¿puede el agente
   correr `pg_dump`?" directamente desaparece.
2. **Usuario dedicado no-root** para el acceso SSH del pipeline, con el script de backup
   vía `sudo` acotado a esa única línea. Recién ahí los permisos de archivo significan algo.
3. **Clave SSH con `command=`** apuntando al script: el agente dispara, no elige qué se
   ejecuta ni ve más que lo que el script imprime.

**El otro motivo para postergarlo** es el mismo del punto 1: agregar esta capa ahora nos
haría más lentos, y la forma del pipeline todavía se está moviendo.

**Mientras tanto:** el backup previo a un pase lo corre un humano. No porque la regla sea
buena, sino porque no hay forma real de restringir al agente mientras tenga root.
