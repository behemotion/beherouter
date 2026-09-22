{{/* beherouter chart helpers. Standard names plus the credential plumbing. */}}

{{- define "beherouter.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "beherouter.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "beherouter.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "beherouter.selectorLabels" -}}
app.kubernetes.io/name: {{ include "beherouter.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "beherouter.labels" -}}
helm.sh/chart: {{ include "beherouter.chart" . }}
{{ include "beherouter.selectorLabels" . }}
{{- with .Chart.AppVersion }}
app.kubernetes.io/version: {{ . | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: beherouter
{{- end -}}

{{- define "beherouter.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "beherouter.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* The Secret the gateway token (and, when created in-chart, the backend
       ${VAR} credentials) is read from. */}}
{{- define "beherouter.secretName" -}}
{{- if .Values.secret.create -}}
{{ include "beherouter.fullname" . }}-env
{{- else -}}
{{- required "existingSecret.name is required when secret.create=false" .Values.existingSecret.name -}}
{{- end -}}
{{- end -}}

{{/* Key of the gateway token inside that Secret. */}}
{{- define "beherouter.tokenKey" -}}
{{- if .Values.secret.create -}}
BEHEROUTER_GATEWAY_TOKEN
{{- else -}}
{{ .Values.existingSecret.gatewayTokenKey }}
{{- end -}}
{{- end -}}

{{/* The lint hook's own Secret. Pre-install hooks run BEFORE release
       resources exist, so the Job cannot reference the release Secret --
       it gets a hook-owned copy when the chart creates one, and otherwise
       falls back to the same existing Secret the gateway reads. */}}
{{- define "beherouter.lintSecretName" -}}
{{- if .Values.secret.create -}}
{{ include "beherouter.fullname" . }}-registry-lint
{{- else -}}
{{ include "beherouter.secretName" . }}
{{- end -}}
{{- end -}}

{{/* BEHEROUTER_PUBLIC_URL: explicit value, else derived from the first
       ingress host (https when TLS is configured, http otherwise). */}}
{{- define "beherouter.publicURL" -}}
{{- $url := .Values.publicURL -}}
{{- if and (not $url) .Values.ingress.enabled -}}
{{- with .Values.ingress.hosts -}}
{{- $scheme := "http" -}}
{{- if $.Values.ingress.tls -}}
{{- $scheme = "https" -}}
{{- end -}}
{{- printf "%s://%s" $scheme (index . 0).host -}}
{{- end -}}
{{- else -}}
{{- $url -}}
{{- end -}}
{{- end -}}

{{/* The gateway container env, shared verbatim by the Deployment and the
       registry-lint hook Job so lint sees the exact environment that will
       serve -- that parity is the point of the pre-deploy gate.

       Context: dict "root" . "secretName" <secret> "tokenKey" <key>. */}}
{{- define "beherouter.env" -}}
- name: BEHEROUTER_REGISTRY
  value: /data/registry.toml
{{- $url := include "beherouter.publicURL" .root | trim -}}
{{- if $url }}
- name: BEHEROUTER_PUBLIC_URL
  value: {{ $url | quote }}
{{- end }}
{{- $auth := .root.Values.auth | default dict }}
{{- $mode := $auth.mode | default "shared" }}
{{- if ne $mode "oidc" }}
- name: BEHEROUTER_GATEWAY_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ .secretName }}
      key: {{ .tokenKey }}
{{- end }}
{{/* ⚠️ ALWAYS rendered, "shared" included. registry-lint SKIPS its auth-mode
       rules when this variable is absent -- deliberately, because lint also runs
       on a workstation where the gateway's env does not exist. Omitting it here
       would therefore let the pre-deploy hook pass a registry the gateway then
       refuses at boot: a surface with [authz] require_roles or [identity] under
       the default mode. Rendering it makes the hook the authority it claims to
       be. */}}
- name: BEHEROUTER_AUTH_MODE
  value: {{ $mode | quote }}
{{- if ne $mode "shared" }}
{{- with $auth.oidc }}
{{- if .issuer }}
- name: BEHEROUTER_OIDC_ISSUER
  value: {{ .issuer | quote }}
{{- end }}
{{- if .audience }}
- name: BEHEROUTER_OIDC_AUDIENCE
  value: {{ .audience | quote }}
{{- end }}
{{- if .jwksUri }}
- name: BEHEROUTER_OIDC_JWKS_URI
  value: {{ .jwksUri | quote }}
{{- end }}
{{- if .requiredScopes }}
- name: BEHEROUTER_OIDC_REQUIRED_SCOPES
  value: {{ .requiredScopes | quote }}
{{- end }}
{{- if .rolesClaim }}
- name: BEHEROUTER_OIDC_ROLES_CLAIM
  value: {{ .rolesClaim | quote }}
{{- end }}
{{- end }}
{{- end }}
{{- if .root.Values.identityMap.enabled }}
- name: BEHEROUTER_IDENTITY_MAP
  value: {{ .root.Values.identityMap.mountPath | quote }}
{{- end }}
{{- if .root.Values.secret.create }}
{{- range $key, $_ := .root.Values.secret.env }}
- name: {{ $key }}
  valueFrom:
    secretKeyRef:
      name: {{ $.secretName }}
      key: {{ $key }}
{{- end }}
{{- end }}
{{- with .root.Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}
