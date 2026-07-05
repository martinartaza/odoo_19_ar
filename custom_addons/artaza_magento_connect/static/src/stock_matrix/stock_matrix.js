/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class MagentoStockMatrix extends Component {
    static template = "artaza_magento_connect.StockMatrix";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.state = useState({
            warehouses: [],
            rows: [],
            search: "",
            loading: false,
        });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        try {
            const data = await this.orm.call(
                "product.product",
                "magento_stock_matrix",
                [this.state.search || ""]
            );
            this.state.warehouses = data.warehouses;
            this.state.rows = data.rows;
        } finally {
            this.state.loading = false;
        }
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
