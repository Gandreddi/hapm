{{- define "hapm.labels" -}}
app.kubernetes.io/part-of: hapm
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
