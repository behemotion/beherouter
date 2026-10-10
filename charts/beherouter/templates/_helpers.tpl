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
{{- if .root.Values.caBundle.enabled }}
- name: SSL_CERT_FILE
  value: /etc/beherouter/ca/ca-bundle.crt
- name: REQUESTS_CA_BUNDLE
  value: /etc/beherouter/ca/ca-bundle.crt
{{- end }}
{{- if include "beherouter.pluginsEnabled" .root }}
- name: PYTHONPATH
  value: {{ .root.Values.plugins.path | quote }}
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


{{/* The gateway image, for every container that runs it (the gateway, the
       lint hook, and the chart's own init containers -- which reuse it so a
       private CA or a plugin install costs no extra image pull). */}}
{{- define "beherouter.image" -}}
{{ required "image.repository is required (default: the published ghcr.io image; override only for a mirror or a self-built image)" .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{/* Non-empty when the `plugins` init container renders: something to install
       from an index (plugins.install) or from a ConfigMap (plugins.local). */}}
{{- define "beherouter.pluginsEnabled" -}}
{{- if or .Values.plugins.install (.Values.plugins.local | default dict).wheels -}}true{{- end -}}
{{- end -}}

{{/* Where plugins.local's ConfigMap is mounted in the init container. The
       ConfigMap's name is IN the path, so plugininstall's "wheel not found"
       names both the ConfigMap and the file. */}}
{{- define "beherouter.pluginsLocalPath" -}}
/etc/beherouter/plugins-local/{{ .Values.plugins.local.configMap }}
{{- end -}}

{{/* plugins.indexes as uv's UV_INDEX: space-separated `name=url`. */}}
{{- define "beherouter.pluginIndexes" -}}
{{- $out := list }}
{{- range . }}{{ $out = append $out (printf "%s=%s" .name .url) }}{{ end }}
{{- join " " $out }}
{{- end -}}

{{/* Init containers, shared VERBATIM by the Deployment and the lint hook:
       the hook must see the same trust store and the same plugins that will
       serve, or it lints a registry against a different gateway. */}}
{{- define "beherouter.initContainers" -}}
{{- if .Values.caBundle.enabled }}
- name: ca-bundle
  image: "{{ include "beherouter.image" . }}"
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  # The system bundle FIRST and kept whole: public endpoints must still verify.
  command: ["sh", "-c"]
  args:
    - |
      set -eu
      out=/etc/beherouter/ca/ca-bundle.crt
      cat "$SYSTEM_BUNDLE" > "$out"
      for f in /etc/beherouter/ca-extra/*; do
        printf '\n' >> "$out"
        cat "$f" >> "$out"
      done
  env:
    - name: SYSTEM_BUNDLE
      value: {{ .Values.caBundle.systemBundle | quote }}
  securityContext:
    {{- toYaml .Values.securityContext | nindent 4 }}
  resources:
    requests: {cpu: 10m, memory: 16Mi}
    limits: {cpu: 100m, memory: 64Mi}
  volumeMounts:
    - name: ca-bundle
      mountPath: /etc/beherouter/ca
    - name: ca-extra
      mountPath: /etc/beherouter/ca-extra
      readOnly: true
{{- end }}
{{- if include "beherouter.pluginsEnabled" . }}
{{- $seen := dict }}
{{- range .Values.plugins.indexes }}
{{- $name := required "every plugins.indexes entry needs a name" .name }}
{{- if not (regexMatch "^[a-z0-9-]+$" $name) }}
{{- fail (printf "plugins.indexes name %q must match [a-z0-9-]+ (it becomes UV_INDEX_<NAME>_*)" $name) }}
{{- end }}
{{- if eq $name "plugins" }}
{{- fail "plugins.indexes name \"plugins\" is reserved: it names plugins.indexUrl" }}
{{- end }}
{{- if hasKey $seen $name }}
{{- fail (printf "duplicate plugins.indexes name %q" $name) }}
{{- end }}
{{- $_ := set $seen $name true }}
{{- $_ := required (printf "plugins.indexes %q needs a url" $name) .url }}
{{- end }}
{{- with .Values.plugins.local.wheels }}
{{- $_ := required "plugins.local.configMap is required when plugins.local.wheels is set" $.Values.plugins.local.configMap }}
{{- range . }}
{{- if or (not (hasSuffix ".whl" .)) (contains "/" .) }}
{{- fail (printf "plugins.local.wheels: %q is not a wheel filename (a plain *.whl key of the ConfigMap)" .) }}
{{- end }}
{{- end }}
{{- end }}
- name: plugins
  image: "{{ include "beherouter.image" . }}"
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  command:
    - python
    - -m
    - beherouter.plugininstall
    - --target
    - {{ .Values.plugins.path | quote }}
    {{- range .Values.plugins.install }}
    - {{ . | quote }}
    {{- end }}
    {{- range .Values.plugins.local.wheels }}
    - {{ printf "%s/%s" (include "beherouter.pluginsLocalPath" $) . | quote }}
    {{- end }}
  env:
    # uv needs somewhere writable; /tmp is the only such place under
    # readOnlyRootFilesystem.
    - name: HOME
      value: /tmp
    - name: UV_CACHE_DIR
      value: /tmp/uv-cache
    {{- with .Values.plugins.indexUrl }}
    # Named, so its credentials can be passed as UV_INDEX_PLUGINS_* rather than
    # embedded in a URL that ends up in `kubectl describe`.
    - name: UV_DEFAULT_INDEX
      value: {{ printf "plugins=%s" . | quote }}
    {{- end }}
    {{- with .Values.plugins.indexCredentialsSecret }}
    - name: UV_INDEX_PLUGINS_USERNAME
      valueFrom:
        secretKeyRef:
          name: {{ . }}
          key: username
    - name: UV_INDEX_PLUGINS_PASSWORD
      valueFrom:
        secretKeyRef:
          name: {{ . }}
          key: password
    {{- end }}
    {{- with .Values.plugins.indexes }}
    # Further named indexes, searched before the default one; uv's default
    # first-index strategy keeps a package on the first index that has it
    # (dependency-confusion protection), so the chart never sets another.
    - name: UV_INDEX
      value: {{ include "beherouter.pluginIndexes" . | quote }}
    {{- range . }}
    {{- if .credentialsSecret }}
    {{- $env := .name | upper | replace "-" "_" }}
    - name: UV_INDEX_{{ $env }}_USERNAME
      valueFrom:
        secretKeyRef:
          name: {{ .credentialsSecret }}
          key: username
    - name: UV_INDEX_{{ $env }}_PASSWORD
      valueFrom:
        secretKeyRef:
          name: {{ .credentialsSecret }}
          key: password
    {{- end }}
    {{- end }}
    {{- end }}
    {{- if .Values.caBundle.enabled }}
    - name: SSL_CERT_FILE
      value: /etc/beherouter/ca/ca-bundle.crt
    {{- end }}
  securityContext:
    {{- toYaml .Values.securityContext | nindent 4 }}
  resources:
    {{- toYaml .Values.plugins.resources | nindent 4 }}
  volumeMounts:
    - name: plugins
      mountPath: {{ .Values.plugins.path }}
    - name: tmp
      mountPath: /tmp
    {{- if .Values.caBundle.enabled }}
    - name: ca-bundle
      mountPath: /etc/beherouter/ca
      readOnly: true
    {{- end }}
    {{- if .Values.plugins.local.wheels }}
    - name: plugins-local
      mountPath: {{ include "beherouter.pluginsLocalPath" . }}
      readOnly: true
    {{- end }}
{{- end }}
{{- with .Values.extraInitContainers }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* Volumes behind the init containers above, plus extraVolumes. */}}
{{- define "beherouter.sharedVolumes" -}}
{{- if .Values.caBundle.enabled }}
- name: ca-bundle
  emptyDir: {}
- name: ca-extra
  configMap:
    name: {{ required "caBundle.configMap is required when caBundle.enabled" .Values.caBundle.configMap }}
    {{- with .Values.caBundle.keys }}
    items:
      {{- range . }}
      - key: {{ . }}
        path: {{ . }}
      {{- end }}
    {{- end }}
{{- end }}
{{- if include "beherouter.pluginsEnabled" . }}
- name: plugins
  emptyDir: {}
{{- end }}
{{- if .Values.plugins.local.wheels }}
# optional: a missing ConfigMap must not hold the pod in ContainerCreating;
# the init container then fails, naming the ConfigMap and the wheel.
- name: plugins-local
  configMap:
    name: {{ .Values.plugins.local.configMap }}
    optional: true
{{- end }}
{{- if .Values.secretFiles.enabled }}
- name: secret-files
  secret:
    secretName: {{ required "secretFiles.secretName is required when secretFiles.enabled=true" .Values.secretFiles.secretName }}
{{- end }}
{{- with .Values.extraVolumes }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* What the gateway (and the lint hook) mount from those volumes -- read-only:
       only the init containers write. */}}
{{- define "beherouter.sharedVolumeMounts" -}}
{{- if .Values.caBundle.enabled }}
- name: ca-bundle
  mountPath: /etc/beherouter/ca
  readOnly: true
{{- end }}
{{- if include "beherouter.pluginsEnabled" . }}
- name: plugins
  mountPath: {{ .Values.plugins.path }}
  readOnly: true
{{- end }}
{{- if .Values.secretFiles.enabled }}
# A directory mount, never subPath: only a directory is updated on rotation.
- name: secret-files
  mountPath: {{ .Values.secretFiles.mountPath }}
  readOnly: true
{{- end }}
{{- with .Values.extraVolumeMounts }}
{{ toYaml . }}
{{- end }}
{{- end -}}
