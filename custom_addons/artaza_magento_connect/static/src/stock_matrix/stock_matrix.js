/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const PAGE_SIZE = 50;

export class MagentoStockMatrix extends Component {
    static template = "artaza_magento_connect.StockMatrix";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.pageSize = PAGE_SIZE;
        this.state = useState({
            warehouses: [],
            rows: [],
            search: "",
            loading: false,
            offset: 0,
            total: 0,
        });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        try {
            const data = await this.orm.call(
                "product.product",
                "magento_stock_matrix",
                [this.state.search || "", this.state.offset, this.pageSize]
            );
            this.state.warehouses = data.warehouses;
            this.state.rows = data.rows;
            this.state.total = data.total;
        } finally {
            this.state.loading = false;
        }
    }

    doSearch() {
        this.state.offset = 0; // toda búsqueda vuelve a la primera página
        this.load();
    }

    prevPage() {
        if (this.canPrev) {
            this.state.offset = Math.max(0, this.state.offset - this.pageSize);
            this.load();
        }
    }

    nextPage() {
        if (this.canNext) {
            this.state.offset += this.pageSize;
            this.load();
        }
    }

    get rangeStart() {
        return this.state.total ? this.state.offset + 1 : 0;
    }
    get rangeEnd() {
        return Math.min(this.state.offset + this.pageSize, this.state.total);
    }
    get canPrev() {
        return this.state.offset > 0;
    }
    get canNext() {
        return this.state.offset + this.pageSize < this.state.total;
    }

    openProduct(row) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "product.product",
            res_id: row.id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    async syncRow(row) {
        const result = await this.orm.call(
            "product.product",
            "magento_sync_stock",
            [[row.id]]
        );
        const pending = (result.skipped || []).length;
        if (pending) {
            this.notification.add(
                `${row.sku}: enviado (${pending} bodega/s pendiente/s en el middleware)`,
                { type: "warning" }
            );
        } else {
            this.notification.add(`${row.sku}: stock sincronizado`, { type: "success" });
        }
    }
}

registry.category("actions").add("artaza_magento_stock_matrix", MagentoStockMatrix);
