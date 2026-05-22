// Slave-side mirror of the CPU's AXI bundle.
module dram (
    input  wire        clk,
    input  wire        rst_n,

    input  wire [31:0] s_axi_araddr,
    input  wire [3:0]  s_axi_arid,
    input  wire [7:0]  s_axi_arlen,
    input  wire [2:0]  s_axi_arsize,
    input  wire        s_axi_arvalid,
    output wire        s_axi_arready,

    output wire [63:0] s_axi_rdata,
    output wire [3:0]  s_axi_rid,
    output wire [1:0]  s_axi_rresp,
    output wire        s_axi_rlast,
    output wire        s_axi_rvalid,
    input  wire        s_axi_rready,

    input  wire [31:0] s_axi_awaddr,
    input  wire [3:0]  s_axi_awid,
    input  wire [7:0]  s_axi_awlen,
    input  wire [2:0]  s_axi_awsize,
    input  wire        s_axi_awvalid,
    output wire        s_axi_awready,

    input  wire [63:0] s_axi_wdata,
    input  wire [7:0]  s_axi_wstrb,
    input  wire        s_axi_wlast,
    input  wire        s_axi_wvalid,
    output wire        s_axi_wready,

    output wire [3:0]  s_axi_bid,
    output wire [1:0]  s_axi_bresp,
    output wire        s_axi_bvalid,
    input  wire        s_axi_bready
);
endmodule
