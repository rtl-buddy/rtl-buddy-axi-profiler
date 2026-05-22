// Top-level connecting one CPU to one DRAM via a shared AXI fabric.
module soc_top (
    input wire clk,
    input wire rst_n
);

    wire [31:0] axi_araddr;
    wire [3:0]  axi_arid;
    wire [7:0]  axi_arlen;
    wire [2:0]  axi_arsize;
    wire        axi_arvalid;
    wire        axi_arready;

    wire [63:0] axi_rdata;
    wire [3:0]  axi_rid;
    wire [1:0]  axi_rresp;
    wire        axi_rlast;
    wire        axi_rvalid;
    wire        axi_rready;

    wire [31:0] axi_awaddr;
    wire [3:0]  axi_awid;
    wire [7:0]  axi_awlen;
    wire [2:0]  axi_awsize;
    wire        axi_awvalid;
    wire        axi_awready;

    wire [63:0] axi_wdata;
    wire [7:0]  axi_wstrb;
    wire        axi_wlast;
    wire        axi_wvalid;
    wire        axi_wready;

    wire [3:0]  axi_bid;
    wire [1:0]  axi_bresp;
    wire        axi_bvalid;
    wire        axi_bready;

    cpu u_cpu (
        .clk(clk), .rst_n(rst_n),
        .m_axi_araddr(axi_araddr), .m_axi_arid(axi_arid),
        .m_axi_arlen(axi_arlen),   .m_axi_arsize(axi_arsize),
        .m_axi_arvalid(axi_arvalid), .m_axi_arready(axi_arready),
        .m_axi_rdata(axi_rdata), .m_axi_rid(axi_rid),
        .m_axi_rresp(axi_rresp), .m_axi_rlast(axi_rlast),
        .m_axi_rvalid(axi_rvalid), .m_axi_rready(axi_rready),
        .m_axi_awaddr(axi_awaddr), .m_axi_awid(axi_awid),
        .m_axi_awlen(axi_awlen),   .m_axi_awsize(axi_awsize),
        .m_axi_awvalid(axi_awvalid), .m_axi_awready(axi_awready),
        .m_axi_wdata(axi_wdata), .m_axi_wstrb(axi_wstrb),
        .m_axi_wlast(axi_wlast),
        .m_axi_wvalid(axi_wvalid), .m_axi_wready(axi_wready),
        .m_axi_bid(axi_bid), .m_axi_bresp(axi_bresp),
        .m_axi_bvalid(axi_bvalid), .m_axi_bready(axi_bready)
    );

    dram u_dram (
        .clk(clk), .rst_n(rst_n),
        .s_axi_araddr(axi_araddr), .s_axi_arid(axi_arid),
        .s_axi_arlen(axi_arlen),   .s_axi_arsize(axi_arsize),
        .s_axi_arvalid(axi_arvalid), .s_axi_arready(axi_arready),
        .s_axi_rdata(axi_rdata), .s_axi_rid(axi_rid),
        .s_axi_rresp(axi_rresp), .s_axi_rlast(axi_rlast),
        .s_axi_rvalid(axi_rvalid), .s_axi_rready(axi_rready),
        .s_axi_awaddr(axi_awaddr), .s_axi_awid(axi_awid),
        .s_axi_awlen(axi_awlen),   .s_axi_awsize(axi_awsize),
        .s_axi_awvalid(axi_awvalid), .s_axi_awready(axi_awready),
        .s_axi_wdata(axi_wdata), .s_axi_wstrb(axi_wstrb),
        .s_axi_wlast(axi_wlast),
        .s_axi_wvalid(axi_wvalid), .s_axi_wready(axi_wready),
        .s_axi_bid(axi_bid), .s_axi_bresp(axi_bresp),
        .s_axi_bvalid(axi_bvalid), .s_axi_bready(axi_bready)
    );

endmodule
