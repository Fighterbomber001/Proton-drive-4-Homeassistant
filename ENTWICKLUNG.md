# Wie die Integration mit der App spricht

Die Integration registriert den Sicherungsort im Backup-Menü von Home Assistant. Sie verwaltet weder den Proton-Login noch einen eigenen Sicherungszeitplan.

- `custom_components/proton_drive_backup/config_flow.py` koppelt die App. Die Installations-ID bindet eine bestehende Kopplung an dieselbe App, auch wenn sich Schlüssel oder interne Adresse ändern.
- `backup.py` übersetzt die Operationen von Home Assistant in Aufrufe an die App. Das Löschen eines Backups wird nicht als unterstützte Operation angekündigt.
- `client.py` transportiert die Daten in Abschnitten. Er folgt keinen HTTP-Weiterleitungen mit dem lokalen Kopplungsschlüssel. Antwortet die App vorzeitig, muss die Integration auch das asynchrone Schließen der Backup-Quelle abwarten.

## Lokal testen

Aus dem Repository-Stamm: `python -B -m unittest discover -s tests -t .` (Python 3.12+ und `aiohttp`). Der Transport wird mit einer lokalen HTTP-Gegenstelle getestet. Ist zusätzlich das Begleit-Repository vorhanden, dessen `proton_drive_backup/rootfs/app` ins `PYTHONPATH` aufnehmen: Dann läuft auch der Test zwischen dem echten Client und der echten HTTP-Schnittstelle der App, ohne Proton-Konto. Zwei weitere Adaptertests benötigen die von ihnen ausdrücklich unterstützte Home-Assistant-Core-Version; ohne diese werden sie übersprungen.

Diese Tests führen keine Sicherung oder Wiederherstellung auf einem produktiven System aus.
