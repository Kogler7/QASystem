/**
 * 与后端 document-preproc /convert 允许的扩展名一致
 * @see backend/app/modules/component/document_preproc/router.py
 */
export const PREPROC_EXTENSIONS = [
  '.pdf',
  '.doc',
  '.docx',
  '.xlsx',
  '.xls',
  '.csv',
  '.html',
  '.htm'
]

export const preprocAcceptAttr = PREPROC_EXTENSIONS.join(',')

export const preprocFormatHint =
  '支持格式：PDF、Word（.doc / .docx）、Excel（.xlsx / .xls）、CSV、网页（.html / .htm）'

export function isPreprocExtension(filename) {
  const parts = (filename || '').split('.')
  if (parts.length < 2) return false
  const ext = '.' + parts.pop().toLowerCase()
  return PREPROC_EXTENSIONS.includes(ext)
}
