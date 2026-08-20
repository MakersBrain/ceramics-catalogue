{{- /*
  Everything under /catalogue in the project's production environment, rendered
  as an environment file. Replace INFISICAL_PROJECT_ID with the project's id.

  listSecrets is not recursive by default, which is what is wanted here:
  /catalogue/backup is rendered separately, to a file only the backup unit
  reads, so the services that need a DSN never hold bucket credentials.
*/ -}}
{{- with listSecrets "INFISICAL_PROJECT_ID" "prod" "/catalogue" }}
{{- range . }}
{{ .Key }}={{ .Value }}
{{- end }}
{{- end }}
