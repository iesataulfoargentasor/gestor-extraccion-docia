# Guion de demostración

Usa documentos sintéticos. No subas expedientes, listas de clase ni las memorias reales del curso.

1. Crea dos cuentas del mismo centro: una colaboradora y una revisora.
2. La colaboradora sube un PDF digital con una actividad, su `event_id` y su edición.
3. La ficha queda `pending`. La hora de recepción no rellena la fecha del evento.
4. Repite la carga del mismo archivo con otro nombre. El resultado es copia exacta: no hay otra versión ni otro trabajo.
5. Cambia el lugar o la fecha y sube otros bytes con el mismo `event_id` y la misma edición. Aparece una candidata. La anterior sigue como estaba.
6. Cambia solo la edición. Es otra identidad, no un duplicado por parecerse.
7. La revisora abre la ficha, escribe el motivo y pulsa «Aprobar para extraer». La colaboradora no puede hacerlo aunque conozca la URL.
8. La misma persona que subió el archivo no puede aceptarlo con la cuenta revisora: hace falta otra cuenta.
9. Arranca `docia-e worker`. Al terminar, la extracción está sin revisar. Reiniciar la web no la convierte en aceptada.
10. La revisora compara el original con las unidades. En un DOCX, `page` va vacío. Un `<script>` dentro del texto se ve como texto.
11. Si falta Tesseract, un escaneo queda en `OCR_UNAVAILABLE` y no se acepta con texto vacío.
12. Con la extracción aceptada, la fuente y la fecha de la actividad, pulsa «Habilitar para T».
13. El consumidor, con el token, lee `/api/etl/v1/handoffs/<id>`.
14. Retira la entrega. La API deja de ofrecerla como lista y `/api/etl/v1/changes` devuelve la revocación en orden de secuencia.
15. `docia-e maintenance --on`, `backup` a otra base y `verify-restore`. La base activa no se toca.

Preguntas para cerrar: dónde está el original, qué distingue una copia de una versión y de una extracción, por qué el OCR no acredita oficialidad y por qué la unidad de esta fase todavía no es un chunk del RAG.
