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
{{- printf "http://%s-ledger:%v" (include "swarm.fullname" .) .Values.ledger.port -}}
{{- end -}}

{{- define "swarm.redisEnv" -}}
{{- if .Values.redis.existingSecret.name }}
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
- name: AGENTIHOOKS_DEPLOYMENT
  value: {{ .Values.deployment | quote }}
- name: LEDGER_URL
  value: {{ include "swarm.ledgerUrl" . | quote }}
- name: SWARM_HIVE_ID
  value: {{ .Values.hive.id | default (include "swarm.fullname" .) | quote }}
{{ include "swarm.redisEnv" . }}
{{- range .Values.secretEnv }}
- name: {{ .name }}
  valueFrom:
    secretKeyRef:
      name: {{ .secretName }}
      key: {{ .key }}
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
    - import os, urllib.request; urllib.request.urlopen("http://127.0.0.1:" + os.environ["LEDGER_PORT"] + "/healthz", timeout=2).read()
{{- end -}}
