# Proton Drive Backup – Integration für Home Assistant

Die Integration fügt **Proton Drive** als zusätzlichen Sicherungsort im normalen Home-Assistant-Backup-Menü hinzu. Du kannst dieses Ziel für geplante oder manuelle Sicherungen auswählen und dort gespeicherte Archive über Home Assistant wieder herunterladen.

Du brauchst zusätzlich die [Proton Drive Backup-App für Home Assistant OS](https://github.com/Fighterbomber001/Proton-drive-4-Homeassistant-App). Sie übernimmt die Proton-Anmeldung, die Übertragung und die Statusanzeige. Die Integration wird über den **Marketplace** installiert; die App kommt aus dem separaten **App-Store**.

**Voraussetzungen:** Home Assistant OS auf amd64 (x86-64), Home Assistant Core ab 2026.11.2, die passende App und ein Proton-Konto mit genügend Platz.

1. Installiere und konfiguriere die App über **Einstellungen → Apps**. Melde dich über ihre Web-Oberfläche bei Proton an.
2. Füge dieses Integrations-Repository über **Einstellungen → Marketplace → Aus Link hinzufügen** hinzu. Installiere die Integration und folge ggf. der Aufforderung zum Core-Neustart.
3. Füge unter **Einstellungen → Geräte & Dienste → Integration hinzufügen** den Eintrag **Proton Drive** hinzu. Verwende die interne Adresse der App und den dort eingerichteten Kopplungsschlüssel.
4. Wähle **Proton Drive** unter **Einstellungen → System → Backups** als zusätzlichen Sicherungsort und prüfe einen manuellen Testlauf mit anschließendem Download.

Das [Handbuch](HANDBUCH.md) führt durch die Einrichtung, den Betrieb und die Wiederherstellung. Lies auch den [Disclaimer](DISCLAIMER.md). Sicherheitsprobleme bitte wie unter [Sicherheitsmeldungen](SECURITY.md) beschrieben melden.

Home Assistant kann die Proton-Kopien über diese Integration nicht löschen. Behalte einen unabhängigen Sicherungsort, sichere den Notfallschlüssel getrennt und erprobe die Wiederherstellung auf einem Testsystem.

Dies ist ein unabhängiges Community-Projekt, kein offizielles Produkt von Proton oder Home Assistant. „Proton“ und „Proton Drive“ sind Marken der Proton AG; siehe [Protons Nutzungsbedingungen](https://proton.me/legal/terms), Abschnitt 8. Die Proton-CLI wird von der separaten App beim Erstellen ihres Images von Proton bezogen und ist nicht Teil des Repository-Quellcodes.
