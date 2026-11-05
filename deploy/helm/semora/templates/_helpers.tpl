{{- define "semora.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "semora.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "semora.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "semora.labels" -}}
app.kubernetes.io/name: {{ include "semora.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end }}

{{- define "semora.selectorLabels" -}}
app.kubernetes.io/name: {{ include "semora.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "semora.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "semora.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "semora.image" -}}
{{- printf "%s:%s" .repository .tag }}
{{- end }}
