/**
 * Shared JavaScript functions for admin pages
 */

// ============ Message Display ============

function showMessage(text, type) {
    const messageEl = document.querySelector(`.message.${type}`);
    if (messageEl) {
        messageEl.textContent = text;
        messageEl.style.display = 'block';
        setTimeout(() => {
            messageEl.style.display = 'none';
        }, 3000);
    }
}

// ============ Tree View State Management ============

function getExpandedNodes() {
    const saved = localStorage.getItem('expandedNodes');
    return saved ? JSON.parse(saved) : [];
}

function saveExpandedNodes(expandedNodes) {
    localStorage.setItem('expandedNodes', JSON.stringify(expandedNodes));
}

function getNodeKey(node) {
    const dataKey = node.getAttribute('data-key');
    if (dataKey) return dataKey;

    // Fallback: generate key from data attributes
    const type = node.getAttribute('data-type');
    const id = node.getAttribute('data-id');
    return type && id ? `${type}:${id}` : null;
}

function toggleNode(header) {
    const node = header.closest('.tree-node');
    if (!node || node.classList.contains('leaf')) return;

    const children = node.querySelector('.node-children');
    if (!children) return;

    const isExpanded = children.style.display !== 'none';
    children.style.display = isExpanded ? 'none' : 'block';

    const icon = header.querySelector('.toggle-icon');
    if (icon) {
        icon.textContent = isExpanded ? '▶' : '▼';
    }

    // Save state
    let expandedNodes = getExpandedNodes();
    const key = getNodeKey(node);

    if (!key) return;

    if (isExpanded) {
        expandedNodes = expandedNodes.filter(k => k !== key);
    } else {
        if (!expandedNodes.includes(key)) {
            expandedNodes.push(key);
        }
    }

    saveExpandedNodes(expandedNodes);
}

function restoreTreeState() {
    const expandedNodes = getExpandedNodes();

    document.querySelectorAll('.tree-node').forEach(node => {
        const key = getNodeKey(node);
        if (!key) return;

        const children = node.querySelector('.node-children');
        if (!children) return;

        const isExpanded = expandedNodes.includes(key);
        children.style.display = isExpanded ? 'block' : 'none';

        const icon = node.querySelector('.toggle-icon');
        if (icon) {
            icon.textContent = isExpanded ? '▼' : '▶';
        }
    });
}

// ============ Table Filtering ============

function filterTable(inputId, tableId) {
    const input = document.getElementById(inputId);
    const table = document.getElementById(tableId);

    if (!input || !table) return;

    const filterText = input.value.toLowerCase();
    const rows = table.querySelectorAll('tbody tr');

    rows.forEach(row => {
        const text = row.textContent.toLowerCase();
        row.style.display = text.includes(filterText) ? '' : 'none';
    });
}

// ============ Modal Management ============

// Note: Specific modal implementations may vary by page
// This provides a basic framework

function openModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.add('active');
    }
}

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.remove('active');
    }
}

// Close modal on Escape key
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        document.querySelectorAll('.modal.active').forEach(modal => {
            modal.classList.remove('active');
        });
    }
});

// Close modal when clicking outside
document.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal')) {
        e.target.classList.remove('active');
    }
});
