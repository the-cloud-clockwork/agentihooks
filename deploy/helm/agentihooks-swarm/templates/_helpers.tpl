{{- define "swarm.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "swarm.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "swarm.selector" -}}
app.kubernetes.io/name: {{ .root.Chart.Name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "swarm.image" -}}
{{- $tag := toString .Values.image.tag -}}
{{- $floating := regexReplaceAll "@sha256:[0-9a-f]{64}$" $tag "" -}}
{{- if or (contains "@" .Values.image.repository) (eq $floating "") (contains "@" $floating) (contains "sha256" $floating) (regexMatch "^[0-9a-f]{40}$" $floating) -}}
{{- fail "image.tag must be a floating tag such as dev, never a digest or commit hash; only the image updater appends @sha256 to it" -}}
{{- end -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}

{{- define "swarm.ledgerUrl" -}}
{{- printf "%s://%s-ledger:%v" (ternary "https" "http" (not (empty .Values.ledger.tls.secretName))) (include "swarm.fullname" .) .Values.ledger.port -}}
{{- end -}}

{{- define "swarm.ledgerCa" -}}
{{- with .Values.ledger.tls.secretName }}
- name: LEDGER_CA_PEM
  valueFrom:
    secretKeyRef:
      name: {{ . | quote }}
      key: ca.crt
{{- end }}
{{- end -}}

{{- define "swarm.redisEnv" -}}
{{- $roles := .Values.redis.roles | default dict -}}
{{- $named := list -}}
{{- range $role := list "ledger" "controller" "worker" }}
{{- if (get $roles $role | default dict).name }}
{{- $named = append $named $role -}}
{{- end }}
{{- end }}
{{- if and $named (ne (len $named) 3) }}
{{- fail "redis.roles sets every role or none: ledger, controller and worker each name their own Secret" }}
{{- end }}
{{- if and $named (ne (toString .role) "hive-join") }}
{{- $ref := get $roles (required "swarm.redisEnv needs a role when redis.roles is set" .role) }}
{{- if eq $ref.name .Values.redis.existingSecret.name }}
{{- fail (printf "redis.roles.%s must not name the administrator Redis Secret %s" .role $ref.name) }}
{{- end }}
- name: AGENTIHOOKS_SWARM_REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ $ref.name }}
      key: {{ required (printf "redis.roles.%s.key names the key holding the Redis URL" .role) $ref.key }}
{{- else if .Values.redis.existingSecret.name }}
- name: AGENTIHOOKS_SWARM_REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.redis.existingSecret.name }}
      key: {{ required "redis.existingSecret.key names the key holding the Redis URL" .Values.redis.existingSecret.key }}
{{- else if .Values.redis.url }}
{{- if contains "@" .Values.redis.url }}
{{- fail "redis.url must not carry credentials; put a credentialed URL in a Secret and set redis.existingSecret" }}
{{- end }}
- name: AGENTIHOOKS_SWARM_REDIS_URL
  value: {{ .Values.redis.url | quote }}
{{- else if .Values.redis.enabled }}
- name: AGENTIHOOKS_SWARM_REDIS_URL
  value: {{ printf "redis://%s-redis:6379/0" (include "swarm.fullname" .) | quote }}
{{- else }}
{{- fail "set redis.enabled, redis.url or redis.existingSecret" }}
{{- end }}
{{- end -}}

{{- define "swarm.env" -}}
{{- $role := .role -}}
{{- with .root -}}
- name: AGENTIHOOKS_DEPLOYMENT
  value: {{ .Values.deployment | quote }}
- name: LEDGER_URL
  value: {{ include "swarm.ledgerUrl" . | quote }}
{{ include "swarm.ledgerCa" . }}
- name: SWARM_HIVE_ID
  value: {{ .Values.hive.id | default (include "swarm.fullname" .) | quote }}
{{ include "swarm.redisEnv" (dict "Values" .Values "Chart" .Chart "Release" .Release "role" $role) }}
{{- range .Values.secretEnv }}
- name: {{ .name }}
  valueFrom:
    secretKeyRef:
      name: {{ .secretName }}
      key: {{ .key }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "swarm.secretKeyEnv" -}}
{{- range . }}
- name: {{ .name }}
  valueFrom:
    secretKeyRef:
      name: {{ .secretName }}
      key: {{ .key }}
      {{- if .optional }}
      optional: true
      {{- end }}
{{- end }}
{{- end -}}

{{- define "swarm.extraEnv" -}}
{{- range $name, $value := . }}
- name: {{ $name }}
  value: {{ $value | quote }}
{{- end }}
{{- end -}}

{{- define "swarm.envFrom" -}}
{{- with .Values.envFromSecrets }}
envFrom:
{{- range . }}
  - secretRef:
      name: {{ . }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "swarm.podSecurity" -}}
automountServiceAccountToken: false
{{ include "swarm.podSecurityContext" . }}
{{- end -}}

{{- define "swarm.podSecurityContext" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  fsGroup: 10001
  seccompProfile:
    type: RuntimeDefault
{{- end -}}

{{- define "swarm.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  capabilities:
    drop: [ALL]
{{- end -}}

{{- define "swarm.healthz" -}}
exec:
  command:
    - python
    - -c
    {{- if .Values.ledger.tls.secretName }}
    - import os, ssl, urllib.request; c = ssl.create_default_context(cadata=os.environ["LEDGER_CA_PEM"]); c.check_hostname = False; urllib.request.urlopen("https://127.0.0.1:" + os.environ["LEDGER_PORT"] + "/healthz", timeout=2, context=c).read()
    {{- else }}
    - import os, urllib.request; urllib.request.urlopen("http://127.0.0.1:" + os.environ["LEDGER_PORT"] + "/healthz", timeout=2).read()
    {{- end }}
{{- end -}}
