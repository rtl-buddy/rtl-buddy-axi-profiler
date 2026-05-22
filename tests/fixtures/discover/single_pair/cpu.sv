// Simple CPU-like module exposing an AXI master interface.
module cpu (
    input  wire        clk,
    input  wire        rst_n,

    output wire [31:0] m_axi_araddr,
    output wire [3:0]  m_axi_arid,
    output wire [7:0]  m_axi_arlen,
    output wire [2:0]  m_axi_arsize,
    output wire        m_axi_arvalid,
    input  wire        m_axi_arready,

    input  wire [63:0] m_axi_rdata,
    input  wire [3:0]  m_axi_rid,
    input  wire [1:0]  m_axi_rresp,
    input  wire        m_axi_rlast,
    input  wire        m_axi_rvalid,
    output wire        m_axi_rready,

    output wire [31:0] m_axi_awaddr,
    output wire [3:0]  m_axi_awid,
    output wire [7:0]  m_axi_awlen,
    output wire [2:0]  m_axi_awsize,
    output wire        m_axi_awvalid,
    input  wire        m_axi_awready,

    output wire [63:0] m_axi_wdata,
    output wire [7:0]  m_axi_wstrb,
    output wire        m_axi_wlast,
    output wire        m_axi_wvalid,
    input  wire        m_axi_wready,

    input  wire [3:0]  m_axi_bid,
    input  wire [1:0]  m_axi_bresp,
    input  wire        m_axi_bvalid,
    output wire        m_axi_bready
);
endmodule
