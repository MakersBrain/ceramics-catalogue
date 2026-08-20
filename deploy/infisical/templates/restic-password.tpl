{{- /*
  restic reads its password from a file path, so this one secret is rendered on
  its own rather than into the environment file. Escrow the same value outside
  Infisical: a repository whose password is only in the system that also holds
  the bucket credentials is not a recoverable backup.
*/ -}}
{{- with secret "INFISICAL_PROJECT_ID" "prod" "/catalogue/backup" }}
{{- range . }}
{{- if eq .Key "RESTIC_PASSWORD" }}{{ .Value }}{{ end }}
{{- end }}
{{- end }}
