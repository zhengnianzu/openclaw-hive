<template>
  <router-view />
</template>

<style>
:root {
  --bg-primary: #f0f2f5;
  --bg-secondary: #ffffff;
  --bg-card: #ffffff;
  --bg-sidebar: #1e293b;
  --bg-hover: #f5f7fa;
  --border-color: #e5e7eb;
  --border-color-light: #f0f0f0;
  --text-primary: #1e293b;
  --text-secondary: #475569;
  --text-muted: #94a3b8;
  --accent-gradient: linear-gradient(135deg, #6366f1, #8b5cf6);
  --accent-purple: #6366f1;
  --accent-blue: #3b82f6;
  --success: #10b981;
  --warning: #f59e0b;
  --danger: #ef4444;
  --info: #6366f1;
  --radius-lg: 16px;
  --radius-md: 12px;
  --radius-sm: 8px;
  --shadow-sm: 0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.06);
  --shadow-md: 0 4px 12px rgba(0,0,0,0.06);
  --shadow-lg: 0 12px 40px rgba(0,0,0,0.1);

  /* Element Plus overrides */
  --el-color-primary: #6366f1;
  --el-color-primary-light-3: #818cf8;
  --el-color-primary-light-5: #a5b4fc;
  --el-color-primary-light-7: #c7d2fe;
  --el-color-primary-light-8: #ddd6fe;
  --el-color-primary-light-9: #ede9fe;
  --el-color-primary-dark-2: #4f46e5;
  --el-color-success: #10b981;
  --el-color-warning: #f59e0b;
  --el-color-danger: #ef4444;
  --el-color-info: #6366f1;
  --el-border-radius-base: 8px;
  --el-font-size-base: 14px;
}

* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg-primary);
  color: var(--text-primary);
  -webkit-font-smoothing: antialiased;
}

/* Glass card utility */
.glass-card {
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-lg);
  padding: 24px;
  box-shadow: var(--shadow-sm);
}

/* Primary gradient button */
.el-button--primary {
  background: var(--accent-gradient) !important;
  border: none !important;
  color: #fff !important;
  font-weight: 500;
  box-shadow: 0 2px 8px rgba(99, 102, 241, 0.25);
  transition: all 0.2s ease;
}
.el-button--primary:hover, .el-button--primary:focus {
  box-shadow: 0 4px 16px rgba(99, 102, 241, 0.35);
  transform: translateY(-1px);
}
.el-button--primary.is-disabled {
  opacity: 0.5;
  box-shadow: none;
  transform: none;
}

/* Table refinement */
.el-table {
  border-radius: var(--radius-md) !important;
  overflow: hidden;
}
.el-table th.el-table__cell {
  background: #f8fafc !important;
  font-weight: 600;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted) !important;
}
.el-table--striped .el-table__body tr.el-table__row--striped td.el-table__cell {
  background: #fafbfc !important;
}
.el-table__row:hover > td.el-table__cell {
  background: #f0f0ff !important;
}

/* Card refinement */
.el-card {
  border-radius: var(--radius-md) !important;
  border: 1px solid var(--border-color) !important;
  box-shadow: var(--shadow-sm) !important;
}
.el-card__header {
  font-weight: 600;
  color: var(--text-primary);
}

/* Dialog refinement */
.el-dialog {
  border-radius: var(--radius-lg) !important;
  box-shadow: var(--shadow-lg) !important;
}

/* Input refinement */
.el-input__wrapper {
  border-radius: var(--radius-sm) !important;
  transition: all 0.2s ease;
}
.el-input__wrapper:hover {
  box-shadow: 0 0 0 1px #c7d2fe inset !important;
}
.el-input__wrapper.is-focus {
  box-shadow: 0 0 0 1px var(--accent-purple) inset, 0 0 0 3px rgba(99, 102, 241, 0.1) inset !important;
}

/* Select dropdown */
.el-select-dropdown__item.is-hovering {
  background: #ede9fe !important;
}
.el-select-dropdown__item.is-selected {
  color: var(--accent-purple) !important;
  font-weight: 600;
}

/* Tag refinement */
.el-tag {
  border-radius: 6px !important;
  font-weight: 500;
}
.el-tag--success {
  background: #ecfdf5 !important;
  color: #059669 !important;
  border-color: #a7f3d0 !important;
}
.el-tag--warning {
  background: #fffbeb !important;
  color: #d97706 !important;
  border-color: #fde68a !important;
}
.el-tag--danger {
  background: #fef2f2 !important;
  color: #dc2626 !important;
  border-color: #fecaca !important;
}
.el-tag--info {
  background: #eef2ff !important;
  color: #4f46e5 !important;
  border-color: #c7d2fe !important;
}

/* Progress bar */
.el-progress-bar__outer {
  border-radius: 6px;
}

/* Statistic */
.el-statistic__head {
  color: var(--text-muted) !important;
}

/* Page header */
.el-page-header__content {
  font-weight: 600;
}
</style>
