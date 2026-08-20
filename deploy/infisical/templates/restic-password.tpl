{{- /*
  restic reads its password from a file path, so this one secret is rendered on
  its own rather than into the environment file. getSecretByName fetches the
  single value: ranging over the whole path and filtering would put every
  backup credential through this template to emit one of them.

  Escrow the same value outside Infisical. A repository whose password is only
  in the system that also holds the bucket credentials is not a recoverable
  backup.
*/ -}}
{{- with getSecretByName "INFISICAL_PROJECT_ID" "prod" "/catalogue/backup" "RESTIC_PASSWORD" }}{{ .Value }}{{ end }}
