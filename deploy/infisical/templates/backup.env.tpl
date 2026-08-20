{{- /*
  The restic repository lives in R2. restic speaks S3, so the endpoint and the
  keys are the whole of the difference; the backup tool reads all of this from
  the environment and never from argv.
*/ -}}
{{- with listSecrets "INFISICAL_PROJECT_ID" "prod" "/catalogue/backup" }}
{{- range . }}
{{ .Key }}={{ .Value }}
{{- end }}
{{- end }}
RESTIC_PASSWORD_FILE=/etc/catalogue/restic-password
