{{- /*
  The restic repository now lives in R2. restic speaks S3, so the only thing
  that changed when it moved off Scaleway is the endpoint and the keys; the
  backup tool itself reads all of this from the environment and never from argv.
*/ -}}
{{- with secret "INFISICAL_PROJECT_ID" "prod" "/catalogue/backup" }}
{{- range . }}
{{ .Key }}={{ .Value }}
{{- end }}
{{- end }}
RESTIC_PASSWORD_FILE=/etc/catalogue/restic-password
