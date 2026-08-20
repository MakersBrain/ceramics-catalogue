{{- /*
  Everything under /catalogue in the project's production environment, rendered
  as an environment file. Replace INFISICAL_PROJECT_ID with the project's id;
  the agent takes an id here, not the slug the CI action uses.
*/ -}}
{{- with secret "INFISICAL_PROJECT_ID" "prod" "/catalogue" }}
{{- range . }}
{{ .Key }}={{ .Value }}
{{- end }}
{{- end }}
